#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
JMComic 下载插件 - 3.1.0
- 修复 PDF 混用多个本子图片的问题
- 下载目录按用户隔离，避免并发下载和清理互相影响
- 清理前校验目标路径，after_send 支持延迟删除
- 封面统一保存到 covers 目录
- 支持配置 cover_keep_days 和 default_pdf_quality
- 自动依赖安装（阿里源）
- 超时60秒处理
"""

import subprocess
import sys

def _ensure_package(package_name, import_name=None):
    if import_name is None:
        import_name = package_name
    try:
        __import__(import_name)
    except ImportError:
        print(f"[JMPlugin] 缺少依赖 {package_name}，正在尝试自动安装...")
        try:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install",
                "-i", "https://mirrors.aliyun.com/pypi/simple/",
                package_name
            ])
            __import__(import_name)
            print(f"[JMPlugin] {package_name} 安装成功")
        except Exception as e:
            print(f"[JMPlugin] 自动安装 {package_name} 失败: {e}")
            print(f"[JMPlugin] 请手动执行: pip install {package_name}")
            raise

_ensure_package("jmcomic")
_ensure_package("img2pdf")
_ensure_package("Pillow", "PIL")

import asyncio
import functools
import os
import shutil
import tempfile
import traceback
import zipfile
import re
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Plain, File
from astrbot.api.message_components import Image as MsgImage

import jmcomic
from jmcomic import (
    JmOption,
    JmAlbumDetail,
    JmModuleConfig,
    JmcomicException,
    MissingAlbumPhotoException,
    RequestRetryAllFailException,
    create_option_by_file,
    download_album,
    DirRule,
    ExceptionTool,
    time_stamp,
    current_thread,
    fix_windir_name,
)
from jmcomic import JmMagicConstants
from jmcomic.jm_downloader import JmDownloader

try:
    import img2pdf
    from PIL import Image as PILImage
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    logger.error("PDF 库未安装，请手动安装: pip install img2pdf Pillow")

DEFAULT_OPTION_FILE = Path(__file__).parent / "assets" / "option" / "option_workflow_download.yml"


@register("jmcomic_downloader", "JMComic 下载", "禁漫下载插件（支持范围下载、图文详情、智能清理）", "3.1.0")
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        download_dir = Path(self.config.get("download_dir", "./data/jm_downloads"))
        if not download_dir.is_absolute():
            base = Path.cwd()
            self.global_base_dir = base / download_dir
        else:
            self.global_base_dir = download_dir
        self.global_base_dir.mkdir(parents=True, exist_ok=True)

        self.cover_dir = self.global_base_dir / "covers"
        self.cover_dir.mkdir(parents=True, exist_ok=True)

        self.option_file = self.config.get("option_file", str(DEFAULT_OPTION_FILE))
        if not Path(self.option_file).exists():
            self.option_file = None
            logger.warning("未找到默认 option 配置文件，使用 jmcomic 内置默认配置")

        self.cleanup_mode = str(self.config.get("cleanup_mode", "count")).lower()
        if self.cleanup_mode not in {"count", "after_send"}:
            logger.warning(f"未知清理模式 {self.cleanup_mode!r}，已回退为 count")
            self.cleanup_mode = "count"
        self.max_albums = max(0, int(self.config.get("max_albums", 10)))
        self.after_send_delay = max(0, int(self.config.get("after_send_delay", 60)))
        self.cover_keep_days = max(0, int(self.config.get("cover_keep_days", 7)))
        self.default_pdf_quality = int(self.config.get("default_pdf_quality", 85))
        self.default_pdf_quality = max(1, min(100, self.default_pdf_quality))

        if not self.config.get("enable_jm_log", False):
            JmModuleConfig.disable_jm_log()

        self._background_tasks = set()
        self._warmup_task = None
        self._need_warmup = True
        self._warmup_task = self._start_task(self._warmup)
        if self._warmup_task is None:
            logger.warning("无事件循环，预热推迟到首次请求")
        self._start_task(self._cleanup_expired_covers)

    def _start_task(self, async_func, *args):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(async_func(*args))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _warmup(self):
        try:
            await asyncio.to_thread(JmModuleConfig.get_html_domain)
            self._need_warmup = False
        except Exception as e:
            logger.warning(f"预热域名失败: {e}")

    def _safe_user_dir(self, user_id: str) -> str:
        safe = re.sub(r'[^a-zA-Z0-9_-]', '_', str(user_id or ""))
        return safe or "unknown_user"

    def _user_base_dir(self, user_id: str, create: bool = False) -> Path:
        user_dir = self.global_base_dir / self._safe_user_dir(user_id)
        if create:
            user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            resolved = path.resolve()
            parent_resolved = parent.resolve()
            return resolved != parent_resolved and parent_resolved in resolved.parents
        except (OSError, RuntimeError):
            return False

    async def _get_option(self, user_id: str = None, cmd_overrides: dict = None) -> Optional['JmOption']:
        if self._need_warmup and (self._warmup_task is None or self._warmup_task.done()):
            self._warmup_task = self._start_task(self._warmup)
        try:
            if self.option_file and Path(self.option_file).exists():
                option = await asyncio.to_thread(create_option_by_file, self.option_file)
            else:
                option = await asyncio.to_thread(JmOption.default)

            option.dir_rule.base_dir = str(self._user_base_dir(user_id, create=True))

            if cmd_overrides:
                self._apply_overrides(option, cmd_overrides)

            return option
        except Exception as e:
            logger.error(f"创建 JmOption 失败: {e}")
            return None

    def _apply_overrides(self, option: 'JmOption', overrides: dict):
        dir_rule = overrides.get('dir_rule')
        if dir_rule:
            option.dir_rule = DirRule(dir_rule, base_dir=option.dir_rule.base_dir)

        impl = overrides.get('client_impl')
        if impl:
            option.client.impl = impl

        suffix = overrides.get('suffix')
        if suffix:
            option.download.image.suffix = suffix if suffix.startswith('.') else f'.{suffix}'

    async def _run_sync(self, func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

    async def _safe_call(self, func, *args, **kwargs):
        try:
            return await self._run_sync(func, *args, **kwargs)
        except MissingAlbumPhotoException as e:
            raise Exception(f"本子/章节不存在: {e}") from e
        except RequestRetryAllFailException as e:
            raise Exception(f"请求重试失败，请稍后重试: {e}") from e
        except JmcomicException as e:
            raise Exception(f"jmcomic 错误: {e}") from e
        except Exception as e:
            logger.error(traceback.format_exc())
            raise Exception(f"未知错误: {e}") from e

    async def _safe_call_with_timeout(self, func, timeout=60, *args, **kwargs):
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, functools.partial(func, *args, **kwargs)),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"操作超时（{timeout}秒）") from None
        except TimeoutError:
            raise
        except Exception as e:
            logger.error(traceback.format_exc())
            raise

    def _create_range_downloader(self, start: int, end: int):
        class RangeDownloader(JmDownloader):
            def do_filter(self, detail):
                if detail.is_album():
                    album_len = len(detail)
                    s = max(0, start - 1)
                    e = min(album_len, end)
                    if s >= e:
                        return []
                    return detail[s:e]
                return detail
        return RangeDownloader

    def _iter_image_files(self, image_dir: Path) -> List[Path]:
        if not image_dir.exists() or image_dir == self.global_base_dir:
            return []
        image_suffixes = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
        return sorted(
            f for f in image_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in image_suffixes
        )

    def _find_album_image_dir(
        self,
        album_id: str,
        preferred_dir: Optional[Path] = None,
        user_id: Optional[str] = None,
        album_title: str = "",
    ) -> Optional[Path]:
        candidates = []
        if preferred_dir:
            candidates.append(preferred_dir)

        search_roots = []
        if user_id:
            search_roots.append(self.global_base_dir / self._safe_user_dir(user_id))
        search_roots.append(self.global_base_dir)

        title_parts = [part for part in re.split(r'\s+', album_title) if len(part) >= 3]
        for root in search_roots:
            if root.exists():
                candidates.append(root)
                for path in root.rglob("*"):
                    if not path.is_dir():
                        continue
                    if album_id in path.name or any(part in path.name for part in title_parts):
                        candidates.append(path)

                for image in self._iter_image_files(root):
                    candidates.append(image.parent)

        if self.cover_dir.exists():
            try:
                cover_dir_resolved = self.cover_dir.resolve()
                candidates = [
                    path for path in candidates
                    if path.resolve() != cover_dir_resolved and cover_dir_resolved not in path.resolve().parents
                ]
            except Exception:
                pass

        now = time.time()
        preferred_root = None
        preferred_dir_resolved = None
        if user_id:
            preferred_root = self.global_base_dir / self._safe_user_dir(user_id)
        if preferred_dir:
            try:
                preferred_dir_resolved = preferred_dir.resolve()
            except Exception:
                pass

        seen = set()
        usable = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved == self.global_base_dir:
                continue
            if preferred_root:
                try:
                    preferred_root_resolved = preferred_root.resolve()
                    if resolved == preferred_root_resolved:
                        continue
                    if resolved != preferred_root_resolved and preferred_root_resolved not in resolved.parents:
                        continue
                except Exception:
                    pass
            images = self._iter_image_files(resolved)
            if images:
                newest = max(f.stat().st_mtime for f in images)
                is_recent = now - newest <= 15 * 60
                title_match = any(part in resolved.name for part in title_parts)
                id_match = album_id in resolved.name
                preferred_match = preferred_dir_resolved == resolved
                score = sum((id_match, title_match, is_recent, preferred_match))
                usable.append((score, len(images), newest, resolved))

        if not usable:
            return None

        usable.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return usable[0][3]

    def _parse_album_command(self, args: List[str], cmd_prefix_len: int) -> Tuple[str, Optional[Tuple[int, int]], Dict[str, Any]]:
        if len(args) <= cmd_prefix_len:
            raise ValueError("缺少本子ID")
        album_id = args[cmd_prefix_len]
        start = end = None
        extra = {}
        i = cmd_prefix_len + 1
        while i < len(args):
            arg = args[i]
            if arg.startswith('--'):
                if '=' in arg:
                    key, value = arg[2:].split('=', 1)
                    extra[key] = value
                elif i + 1 < len(args) and not args[i+1].startswith('--'):
                    extra[arg[2:]] = args[i+1]
                    i += 1
                else:
                    extra[arg[2:]] = True
            else:
                if '-' in arg:
                    parts = arg.split('-')
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        start = int(parts[0])
                        end = int(parts[1])
                elif arg.isdigit():
                    start = end = int(arg)
            i += 1
        return album_id, (start, end) if start is not None else None, extra

    @filter.command("jm download")
    async def command_jm_download(self, event: AstrMessageEvent):
        if not PDF_AVAILABLE:
            yield event.plain_result("PDF 库未安装，无法生成 PDF")
            return
        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result("请提供本子ID，例如：/jm download 123")
            return
        try:
            album_id, range_tuple, extra = self._parse_album_command(args, 2)
        except ValueError as e:
            yield event.plain_result(str(e))
            return

        overrides = {}
        if range_tuple:
            overrides['chapter_range'] = range_tuple
            start, end = range_tuple
            yield event.plain_result(f"开始下载 {album_id} 第{start}~{end}章")
        else:
            yield event.plain_result(f"开始下载 {album_id}")
        self._start_task(self._download_album_task, event, album_id, False, overrides, extra)

    @filter.command("jmz")
    async def command_jmz(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("请提供本子ID，例如：/jmz 123")
            return
        try:
            album_id, range_tuple, extra = self._parse_album_command(args, 1)
        except ValueError as e:
            yield event.plain_result(str(e))
            return

        overrides = {}
        if range_tuple:
            overrides['chapter_range'] = range_tuple
            start, end = range_tuple
            yield event.plain_result(f"开始打包 {album_id} 第{start}~{end}章")
        else:
            yield event.plain_result(f"开始打包 {album_id}")
        self._start_task(self._download_album_task, event, album_id, True, overrides, extra)

    @filter.command("jms")
    async def command_jms(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("请提供搜索关键词")
            return
        keyword = args[1]
        page = 1
        if len(args) >= 3 and args[2].isdigit():
            page = int(args[2])
        yield event.plain_result(f"搜索「{keyword}」第{page}页")
        self._start_task(self._do_search, event, keyword, page)

    @filter.command("jmr")
    async def command_jmr(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        rank_type = "month"
        page = 1
        if len(args) >= 2:
            if args[1].lower() in ("week", "day"):
                rank_type = args[1].lower()
            if len(args) >= 3 and args[2].isdigit():
                page = int(args[2])
        yield event.plain_result(f"获取{rank_type}榜第{page}页")
        self._start_task(self._do_ranking, event, rank_type, page)

    @filter.command("jm detail")
    async def command_detail(self, event: AstrMessageEvent):
        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result("请提供本子ID")
            return
        album_id = args[2]
        async for ret in self._do_detail(event, album_id):
            yield ret

    @filter.command("jm help")
    async def command_help(self, event: AstrMessageEvent):
        help_text = """
【禁漫下载插件使用说明】

/jm download <本子号> [范围] [--quality=80] [--max-size=1920]
    下载本子，生成PDF。范围示例：1-10 或 5，压缩参数可选。
/jmz <本子号> [范围]         下载并打包ZIP
/jms <关键词> [页码]         搜索
/jmr [week|day] [页码]       排行榜
/jm detail <本子号>          查看详情
/jm help                     本帮助
        """.strip()
        yield event.plain_result(help_text)

    async def _generate_compressed_pdf(self, image_dir: Path, output_pdf: Path, quality: int = None, max_size: int = 0) -> bool:
        if not PDF_AVAILABLE:
            logger.error("PDF 库未安装")
            return False

        if quality is None:
            quality = self.default_pdf_quality
        quality = max(1, min(100, quality))

        if not image_dir.exists():
            logger.error(f"图片目录不存在: {image_dir.resolve()}")
            return False

        # 防止误用根目录
        if image_dir == self.global_base_dir:
            logger.error("图片目录被设置为根目录，终止PDF生成")
            return False

        try:
            image_files = self._iter_image_files(image_dir)
            logger.info(f"在 {image_dir.resolve()} 找到 {len(image_files)} 个图片文件")
            if image_files:
                logger.debug(f"前5个文件: {[f.name for f in image_files[:5]]}")
        except Exception as e:
            logger.error(f"扫描图片目录失败: {e}")
            return False

        if not image_files:
            logger.warning("没有找到支持的图片")
            return False

        tmpdir_obj = tempfile.TemporaryDirectory(prefix="jm_pdf_")
        tmpdir = tmpdir_obj.name
        try:
            tmp_paths = []
            for i, img_path in enumerate(image_files):
                try:
                    img = PILImage.open(img_path)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    if max_size > 0:
                        img.thumbnail((max_size, max_size), PILImage.Resampling.LANCZOS)
                    out_name = f"{img_path.stem}_{i}.jpg"
                    out_path = Path(tmpdir) / out_name
                    img.save(out_path, "JPEG", quality=quality, optimize=True)
                    tmp_paths.append(str(out_path))
                except Exception as e:
                    logger.error(f"处理图片失败 {img_path}: {e}")
                    return False

            logger.info(f"临时图片已保存至 {tmpdir}，共 {len(tmp_paths)} 个")

            try:
                with open(output_pdf, "wb") as f:
                    f.write(img2pdf.convert(tmp_paths))
                logger.info(f"PDF 生成成功: {output_pdf.resolve()}")
                return True
            except Exception as e:
                logger.error(f"img2pdf 转换失败: {e}")
                return False
        finally:
            try:
                tmpdir_obj.cleanup()
            except Exception as e:
                logger.warning(f"清理临时目录失败: {e}")

    async def _download_album_task(self, event: AstrMessageEvent, album_id: str, pack: bool, overrides: dict, extra: dict):
        user_id = event.get_sender_id()
        sent_files = []
        try:
            option = await self._get_option(user_id, overrides)
            if option is None:
                await event.send(event.plain_result("无法创建下载配置"))
                return

            chapter_range = overrides.get('chapter_range')
            downloader_class = None
            if chapter_range:
                start, end = chapter_range
                downloader_class = self._create_range_downloader(start, end)

            result = await self._safe_call(download_album, album_id, option, downloader=downloader_class)
            if isinstance(result, tuple) and len(result) == 2:
                album, downloader = result
            else:
                album = result
                downloader = None

            # 通过第一个章节的图片目录推断本子根目录
            if len(album) == 0:
                await event.send(event.plain_result("本子无章节，无法确定图片目录"))
                return

            first_photo = album[0]
            first_photo_dir = Path(option.decide_image_save_dir(first_photo)).resolve()
            logger.info(f"第一个章节图片目录: {first_photo_dir}")
            user_base_dir = self._user_base_dir(user_id)

            # 判断是否有章节子文件夹
            if first_photo_dir.parent == user_base_dir.resolve():
                # 图片直接保存在本子目录下（无章节子文件夹）
                album_dir = first_photo_dir
            else:
                # 有章节子文件夹，本子目录为父目录
                album_dir = first_photo_dir.parent

            logger.info(f"最终图片下载目录: {album_dir}")

            # 安全校验
            if album_dir in {self.global_base_dir.resolve(), user_base_dir.resolve()}:
                logger.error("仍然无法确定正确的本子目录，终止下载")
                await event.send(event.plain_result("无法确定本子图片目录，请检查配置"))
                return

            if not album_dir.exists():
                logger.error(f"图片目录不存在: {album_dir}")
                await event.send(event.plain_result("图片目录未创建，下载可能失败"))
                return

            resolved_album_dir = self._find_album_image_dir(album_id, album_dir, user_id, getattr(album, "title", ""))
            if resolved_album_dir is None:
                logger.warning(f"没有找到图片，推断目录: {album_dir}, 下载根目录: {self.global_base_dir}")
                await event.send(event.plain_result("下载完成但没有找到图片，请稍后重试或检查 jmcomic 配置"))
                return

            if resolved_album_dir != album_dir:
                logger.info(f"使用兜底找到的图片目录: {resolved_album_dir}")
                album_dir = resolved_album_dir

            if pack:
                zip_path = await self._handle_zip_result(event, album_id, album_dir)
                if zip_path:
                    sent_files.append(zip_path)
            else:
                quality = int(extra.get('quality', self.default_pdf_quality))
                max_size = int(extra.get('max-size', 0))

                pdf_dir = self._user_base_dir(user_id, create=True) / "pdfs"
                pdf_dir.mkdir(parents=True, exist_ok=True)
                pdf_path = pdf_dir / f"{album_id}.pdf"

                success = await self._generate_compressed_pdf(album_dir, pdf_path, quality, max_size)
                if success:
                    await event.send(event.chain_result([
                        Plain(f"本子 {album_id} 下载完成，已转换为 PDF（质量={quality}）："),
                        File(file=str(pdf_path), name=pdf_path.name)
                    ]))
                    sent_files.append(pdf_path)
                else:
                    await event.send(event.plain_result("PDF 生成失败，请查看日志"))

            # 清理逻辑
            if sent_files and self.cleanup_mode == "after_send":
                self._start_task(self._delete_after_send, user_id, album_dir, sent_files)
            elif self.cleanup_mode == "count":
                self._start_task(self._cleanup_old_albums, user_id)

        except Exception as e:
            logger.error(f"下载任务异常: {traceback.format_exc()}")
            await event.send(event.plain_result(f"下载失败: {e}"))

    async def _handle_zip_result(self, event: AstrMessageEvent, item_id: str, folder: Path) -> Optional[Path]:
        if not folder.exists() or not any(folder.iterdir()):
            await event.send(event.plain_result("下载完成但文件夹为空"))
            return None

        zip_path = folder.with_suffix(".zip")
        try:
            await self._run_sync(self._zip_folder, folder, zip_path)
            if zip_path.exists():
                await event.send(event.chain_result([
                    Plain(f"ID {item_id} 下载完成，打包文件："),
                    File(file=str(zip_path), name=zip_path.name)
                ]))
                return zip_path
            else:
                await event.send(event.plain_result("打包失败"))
                return None
        except Exception as e:
            logger.error(f"压缩失败: {e}")
            await event.send(event.plain_result("打包失败"))
            return None

    @staticmethod
    def _zip_folder(folder: Path, zip_path: Path):
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, start=folder.parent)
                    zipf.write(file_path, arcname)

    async def _delete_after_send(self, user_id: str, album_dir: Path, sent_files: List[Path]):
        if self.after_send_delay > 0:
            await asyncio.sleep(self.after_send_delay)

        user_dir = self._user_base_dir(user_id)
        targets = [album_dir, *sent_files]
        await self._run_sync(self._delete_paths_safely, user_dir, targets)

    def _delete_paths_safely(self, user_dir: Path, targets: List[Path]):
        for target in targets:
            if not self._is_within(target, user_dir):
                logger.error(f"拒绝清理用户目录以外的路径: {target}")
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                logger.info(f"已清理: {target}")
            except Exception as e:
                logger.error(f"清理失败 {target}: {e}")

    async def _cleanup_old_albums(self, user_id: str):
        if self.cleanup_mode != "count" or self.max_albums <= 0:
            return

        user_dir = self._user_base_dir(user_id)
        if not user_dir.exists():
            return
        exclude_dirs = {"pdfs", "logs"}
        try:
            album_dirs = [d for d in user_dir.iterdir() if d.is_dir() and d.name not in exclude_dirs]
        except Exception as e:
            logger.error(f"读取用户下载目录失败 {user_dir}: {e}")
            return

        album_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        to_delete = album_dirs[self.max_albums:]
        targets = [path for folder in to_delete for path in (folder, folder.with_suffix(".zip"))]

        pdf_dir = user_dir / "pdfs"
        if pdf_dir.exists():
            pdf_files = sorted(
                (p for p in pdf_dir.glob("*.pdf") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            targets.extend(pdf_files[self.max_albums:])

        zip_files = sorted(
            (p for p in user_dir.glob("*.zip") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        targets.extend(zip_files[self.max_albums:])
        await self._run_sync(self._delete_paths_safely, user_dir, list(dict.fromkeys(targets)))

    async def _do_search(self, event: AstrMessageEvent, keyword: str, page: int):
        try:
            option = await self._get_option(event.get_sender_id())
            if option is None:
                await event.send(event.plain_result("无法创建下载配置"))
                return
            client = option.build_jm_client()
            search_kwargs = {
                'search_query': keyword,
                'page': page,
                'main_tag': 0,
                'order_by': JmMagicConstants.ORDER_BY_LATEST,
                'time': JmMagicConstants.TIME_ALL,
                'category': JmMagicConstants.CATEGORY_ALL,
                'sub_category': None
            }
            search_page = await self._safe_call_with_timeout(client.search, timeout=60, **search_kwargs)
            content = search_page.content if hasattr(search_page, 'content') else list(search_page) if search_page else []
            if not content:
                await event.send(event.plain_result("没有找到相关本子"))
                return
            lines = [f"搜索「{keyword}」结果（第{page}/{getattr(search_page, 'page_count', 1)}页）："]
            for idx, (aid, info) in enumerate(content[:10], 1):
                title = info.get('name', '未知标题') if isinstance(info, dict) else str(info)
                lines.append(f"{idx}. ID: {aid} | {title}")
            lines.append(f"共{getattr(search_page, 'total', len(content))}条")
            await event.send(event.plain_result("\n".join(lines)))
        except (asyncio.TimeoutError, TimeoutError):
            await event.send(event.plain_result("搜索超时，请稍后重试"))
        except Exception as e:
            logger.error(traceback.format_exc())
            await event.send(event.plain_result(f"搜索失败: {e}"))

    async def _do_ranking(self, event: AstrMessageEvent, rank_type: str, page: int):
        try:
            option = await self._get_option(event.get_sender_id())
            if option is None:
                await event.send(event.plain_result("无法创建下载配置"))
                return
            client = option.build_jm_client()
            if rank_type == "month":
                result = await self._safe_call_with_timeout(client.month_ranking, page=page, timeout=60)
            elif rank_type == "week":
                result = await self._safe_call_with_timeout(client.week_ranking, page=page, timeout=60)
            else:
                result = await self._safe_call_with_timeout(client.day_ranking, page=page, timeout=60)
            content = result.content if hasattr(result, 'content') else list(result) if result else []
            if not content:
                await event.send(event.plain_result("暂无数据"))
                return
            lines = [f"{rank_type}榜 第{page}页："]
            for idx, (aid, info) in enumerate(content[:10], 1):
                title = info.get('name', '未知标题') if isinstance(info, dict) else str(info)
                lines.append(f"{idx}. ID: {aid} | {title}")
            lines.append(f"共{len(content)}条")
            await event.send(event.plain_result("\n".join(lines)))
        except (asyncio.TimeoutError, TimeoutError):
            await event.send(event.plain_result("获取排行榜超时，请稍后重试"))
        except Exception as e:
            logger.error(traceback.format_exc())
            await event.send(event.plain_result(f"获取排行榜失败: {e}"))

    async def _cleanup_expired_covers(self):
        if self.cover_keep_days <= 0:
            return
        while True:
            try:
                if self.cover_dir.exists():
                    cutoff = datetime.now() - timedelta(days=self.cover_keep_days)
                    for file in self.cover_dir.glob("*.jpg"):
                        mtime = datetime.fromtimestamp(file.stat().st_mtime)
                        if mtime < cutoff:
                            file.unlink()
                            logger.debug(f"已删除过期封面: {file}")
            except Exception as e:
                logger.error(f"清理封面出错: {e}")
            await asyncio.sleep(86400)

    async def _do_detail(self, event: AstrMessageEvent, album_id: str):
        cover_path = None
        try:
            option = await self._get_option(event.get_sender_id())
            if option is None:
                await event.send(event.plain_result("无法创建下载配置"))
                return
            client = option.build_jm_client()
            album: 'JmAlbumDetail' = await self._safe_call(client.get_album_detail, album_id)

            lines = [
                f"标题：{album.title}",
                f"作者：{album.author}",
                f"收藏数：{album.likes}",
                f"章节数：{len(album)}",
            ]
            if album.tags:
                lines.append(f"标签：{'、'.join(album.tags)}")
            else:
                lines.append("标签：无")

            if len(album) > 0:
                lines.append("章节列表：")
                for idx, photo in enumerate(album):
                    if idx >= 10:
                        lines.append(f"  ... 还有 {len(album)-10} 个章节")
                        break
                    lines.append(f"  {idx+1}. ID: {photo.photo_id} | {photo.name}")
            else:
                lines.append("该本子暂无章节")

            message_chain = [Plain("\n".join(lines))]

            try:
                cover_filename = f"cover_{album_id}_{int(time.time())}.jpg"
                cover_path = self.cover_dir / cover_filename

                await self._run_sync(client.download_album_cover, album_id, str(cover_path))
                cover_path.chmod(0o644)
                message_chain.append(MsgImage.fromFileSystem(str(cover_path)))
                logger.info(f"封面已保存到: {cover_path}")
            except Exception as e:
                logger.warning(f"下载封面失败: {e}")

            yield event.chain_result(message_chain)

        except Exception as e:
            await event.send(event.plain_result(f"获取详情失败: {e}"))

    async def terminate(self):
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("禁漫插件已卸载")
