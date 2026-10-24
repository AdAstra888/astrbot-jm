#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
JMComic & PicACG 下载插件 - 3.5.1

3.5.0 主要变更（新增哔咔漫画）：
- 新增 /pica 命令族（下载 / 章节 / 搜索 / 详情 / 排行榜 / 随机 / 收藏 / 停止）
- 哔咔与 JM 共用并发闸门、PDF 缓存、清理策略和发送限制
- 哔咔配置：pica_account、pica_password、pica_token、pica_proxy、pica_concurrency
- 哔咔实现位于 pica/ 子包，登录后的 token 缓存在 cache/pica_token.json

3.4.0 主要变更（随机推荐与更简洁的帮助）：
- 新增 /jm rand，从榜单或搜索结果随机推荐，自动跳过已发过的本子
- 已发记录保存在下载目录的 sent_albums.json，最多保留 1000 条
- /jm help 按使用频率重排，只保留最常用的命令

3.3.0 主要变更（进度/停止/定时清理/操作简化）：
- 下载过程按章节/页数推送进度消息（可配置开关与最小间隔）
- 新增 /jm stop 停止当前下载（基于 jmcomic DownloadControl 协作取消）
- 新增 auto_cleanup_days 定时清理与 /jm clean 手动清理
- 只注册一个 /jm 命令 + 子命令（download/zip/search/rank/detail/stop/clean/help），
  避免多词命令在部分 AstrBot 版本匹配不到的问题
- 兼容保留 /jmz、/jms、/jmr 三个旧命令

3.2.0 主要变更（面向小群/个人使用）：
- 下载任务增加全局并发上限、同用户任务上限、同本子互斥锁和冷却时间
- PDF 生成移出事件循环（asyncio.to_thread），不再卡死整个 Bot
- 图片目录改为按章节对象精确推导，不再全盘扫描猜目录，避免发错本子或误删
- PDF 页序改为按章节顺序 + 自然排序，修复 1/10/2 乱序
- 相同本子/参数跨用户复用 PDF 缓存，重复请求不再重新下载
- 发送前检查文件大小，超限自动降级清晰度并给出明确提示
- 移除导入期自动 pip 安装，依赖改由 requirements.txt 管理
- 修复 ZIP 文件名被 with_suffix 截断的问题
"""

import asyncio
import inspect
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import traceback
import weakref
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.message_components import Image as MsgImage
from astrbot.api.star import Context, Star, register

try:
    from .pica.plugin import PicaMixin
except ImportError:  # 兼容以单文件方式加载的场景
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pica.plugin import PicaMixin

try:
    import jmcomic  # noqa: F401  (供 create_option_by_file 等间接使用)
    from jmcomic import (
        DirRule,
        JmAlbumDetail,
        JmMagicConstants,
        JmModuleConfig,
        JmOption,
        JmcomicException,
        MissingAlbumPhotoException,
        RequestRetryAllFailException,
        create_option_by_file,
        download_album,
    )
    from jmcomic.jm_downloader import JmDownloader
    try:  # 2.7+ 才有协作取消能力，老版本降级为“只能等它跑完”
        from jmcomic import DownloadCancelledException, DownloadControl
    except ImportError:  # pragma: no cover
        class DownloadCancelledException(Exception):
            """老版本 jmcomic 的占位异常。"""

        DownloadControl = None
except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
    raise ImportError(
        "缺少 jmcomic 依赖，请在 AstrBot 的 Python 环境中执行："
        "pip install -r requirements.txt"
    ) from exc

try:
    import img2pdf
    from PIL import Image as PILImage
    from PIL import ImageOps

    PDF_AVAILABLE = True
except ImportError:  # pragma: no cover
    img2pdf = None
    PILImage = None
    ImageOps = None
    PDF_AVAILABLE = False
    logger.error("PDF 库未安装，请执行: pip install img2pdf Pillow")

VERSION = "3.5.1"
BASE_DIR = Path(__file__).resolve().parent
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_NATURAL_SPLIT = re.compile(r"(\d+)")

try:
    _DOWNLOAD_ALBUM_HAS_CONTROL = "control" in inspect.signature(download_album).parameters
except (TypeError, ValueError):  # pragma: no cover
    _DOWNLOAD_ALBUM_HAS_CONTROL = False


class _JobCancelled(Exception):
    """插件内部的“用户已停止”信号。"""


class _ProgressReporter:
    """下载进度上报器：在 jmcomic 工作线程中调用，回到事件循环发消息。"""

    def __init__(
        self,
        plugin: "JmComicPlugin",
        event: AstrMessageEvent,
        album_id: str,
        chapter_range: Optional[Tuple[int, int]],
        interval: int,
    ):
        self.plugin = plugin
        self.event = event
        self.album_id = album_id
        self.chapter_range = chapter_range
        self.interval = max(1, interval)
        self.loop = asyncio.get_running_loop()
        self.enabled = plugin.progress_updates
        self._last_emit = 0.0
        self.total_photos = 0
        self.total_images = 0
        self.done_photos = 0
        self.done_images = 0

    def _emit(self, text: str):
        if not self.enabled:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.plugin._safe_send(self.event, text), self.loop
            )
        except RuntimeError:  # 事件循环已关闭（插件卸载）
            pass

    def _throttled(self) -> bool:
        now = time.monotonic()
        if now - self._last_emit < self.interval:
            return False
        self._last_emit = now
        return True

    def start_album(self, album):
        try:
            photos = self.plugin._select_photos(album, self.chapter_range)
            self.total_photos = len(photos)
            self.total_images = sum(len(photo) for photo in photos)
            title = str(getattr(album, "title", "") or "").strip()[:40] or self.album_id
            self._last_emit = time.monotonic()
            self._emit(
                f"开始下载《{title}》\n"
                f"共 {self.total_photos} 章 / {self.total_images} 页"
                f"（发送「/jm stop」可停止）"
            )
        except Exception as exc:  # 进度失败绝不影响下载
            logger.debug(f"初始化进度失败: {exc}")

    def on_photo(self, photo):
        self.done_photos += 1
        if self.total_photos <= 1:  # 单章本子按页报进度
            return
        if self._throttled():
            percent = int(self.done_photos * 100 / max(1, self.total_photos))
            self._emit(f"进度 {self.done_photos}/{self.total_photos} 章（{percent}%）")

    def on_image(self):
        self.done_images += 1
        if self.total_photos > 1 or self.total_images <= 0:
            return
        if self._throttled():
            percent = int(self.done_images * 100 / max(1, self.total_images))
            self._emit(f"进度 {self.done_images}/{self.total_images} 页（{percent}%）")

    def finish(self, text: str):
        self._emit(text)


@register("jmcomic_downloader", "JMComic 下载", "禁漫下载插件（支持范围下载、图文详情、智能清理）", VERSION)
class JmComicPlugin(PicaMixin, Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        self.global_base_dir = self._resolve_download_dir()
        self.global_base_dir.mkdir(parents=True, exist_ok=True)

        self.cover_dir = self.global_base_dir / "covers"
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_cache_dir = self.global_base_dir / "cache" / "pdfs"
        self.pdf_cache_dir.mkdir(parents=True, exist_ok=True)

        self.option_file = str(self.config.get("option_file", "") or "").strip()
        if self.option_file:
            option_path = Path(self.option_file).expanduser()
            if not option_path.is_absolute():
                option_path = BASE_DIR / option_path
            if option_path.exists():
                self.option_file = str(option_path)
            else:
                logger.warning(f"自定义 option 文件不存在，回退到 jmcomic 内置默认配置: {option_path}")
                self.option_file = ""

        self.cleanup_mode = str(self.config.get("cleanup_mode", "count")).lower()
        if self.cleanup_mode not in {"count", "after_send"}:
            logger.warning(f"未知清理模式 {self.cleanup_mode!r}，已回退为 count")
            self.cleanup_mode = "count"

        self.max_albums = self._config_int("max_albums", 10, 0)
        self.after_send_delay = self._config_int("after_send_delay", 10, 0)
        self.cover_keep_days = self._config_int("cover_keep_days", 7, 0)
        self.max_concurrent = self._config_int("max_concurrent", 1, 1)
        self.cooldown_seconds = self._config_int("cooldown_seconds", 30, 0)
        self.max_file_size_mb = self._config_int("max_file_size_mb", 90, 0)
        self.pdf_cache_max = self._config_int("pdf_cache_max", 30, 0)
        self.min_free_disk_mb = self._config_int("min_free_disk_mb", 200, 0)
        self.default_pdf_quality = max(1, min(100, self._config_int("default_pdf_quality", 85, 1)))
        self.allowed_groups = self._config_str_set("allowed_groups")
        self.progress_updates = bool(self.config.get("progress_updates", True))
        self.progress_interval = self._config_int("progress_interval", 8, 3)
        self.auto_cleanup_days = self._config_int("auto_cleanup_days", 0, 0)
        self.sent_albums_path = self.global_base_dir / "sent_albums.json"
        self._sent_albums: Dict[str, float] = self._load_sent_albums()

        if not self.config.get("enable_jm_log", False):
            JmModuleConfig.disable_jm_log()

        self._background_tasks: set = set()
        self._warmup_task: Optional[asyncio.Task] = None
        self._need_warmup = True
        self._sem: Optional[asyncio.Semaphore] = None
        self._album_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()
        self._user_jobs: Dict[str, int] = {}
        self._last_request: Dict[str, float] = {}
        self._running_downloads = 0
        self._warned_no_bd = False
        self._active_jobs: Dict[str, Dict[str, Any]] = {}
        self._cancel_requests: set = set()

        self._warmup_task = self._start_task(self._warmup)
        if self._warmup_task is None:
            logger.warning("无事件循环，预热推迟到首次请求")
        self._start_task(self._maintenance_loop)

    # ------------------------------------------------------------------
    # 配置与基础设施
    # ------------------------------------------------------------------
    def _resolve_download_dir(self) -> Path:
        raw = str(self.config.get("download_dir", "./data/jm_downloads") or "./data/jm_downloads")
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path
        return (Path.cwd() / path).resolve()

    def _config_int(self, key: str, default: int, minimum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"配置项 {key} 不是整数，使用默认值 {default}")
            value = default
        return max(minimum, value)

    def _config_str_set(self, key: str) -> set:
        raw = self.config.get(key, [])
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _load_sent_albums(self) -> Dict[str, float]:
        """读取「已经发过的本子」记录，用于随机推荐去重。"""
        try:
            if not self.sent_albums_path.is_file():
                return {}
            data = json.loads(self.sent_albums_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"读取已发送记录失败，将重新开始记录: {exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        result: Dict[str, float] = {}
        for key, value in data.items():
            key = str(key)
            if not key.isdigit():
                continue
            try:
                result[key] = float(value)
            except (TypeError, ValueError):
                result[key] = 0.0
        return result

    def _save_sent_albums(self):
        try:
            payload = json.dumps(self._sent_albums, ensure_ascii=False)
            temp_path = self.sent_albums_path.with_name(self.sent_albums_path.name + ".part")
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, self.sent_albums_path)
        except OSError as exc:
            logger.warning(f"保存已发送记录失败: {exc}")

    def _mark_album_sent(self, album_id: str):
        """记录已发送的本子；超过 1000 条时按时间淘汰最旧的。"""
        album_id = str(album_id).strip()
        # JM 的本子 ID 是纯数字；哔咔的是 24 位十六进制，统一加 pica_ 前缀隔离
        if not re.fullmatch(r"(?:\d+|pica_[0-9a-f]{6,})", album_id, flags=re.IGNORECASE):
            return
        self._sent_albums[album_id] = time.time()
        if len(self._sent_albums) > 1000:
            overflow = len(self._sent_albums) - 1000
            oldest = sorted(self._sent_albums.items(), key=lambda item: item[1])[:overflow]
            for key, _ in oldest:
                self._sent_albums.pop(key, None)
        self._save_sent_albums()

    def _start_task(self, async_func, *args):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self._run_background(async_func, args))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _run_background(self, async_func, args):
        """后台任务的统一兜底：异常写日志，并尽量把错误回给用户，避免「发完就没反应」。"""
        try:
            return await async_func(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"后台任务 {getattr(async_func, '__name__', async_func)} 执行失败: "
                f"{traceback.format_exc()}"
            )
            event = args[0] if args else None
            if hasattr(event, "send"):
                try:
                    await self._safe_send(event, f"命令执行出错: {exc}")
                except Exception:  # pragma: no cover
                    pass
            return None

    async def _warmup(self):
        try:
            await asyncio.to_thread(JmModuleConfig.get_html_domain)
            self._need_warmup = False
        except Exception as exc:
            logger.warning(f"预热域名失败: {exc}")

    def _get_sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.max_concurrent)
        return self._sem

    def _get_album_lock(self, key: str) -> asyncio.Lock:
        lock = self._album_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._album_locks[key] = lock
        return lock

    def _safe_user_dir(self, user_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(user_id or ""))
        return safe or "unknown_user"

    def _user_base_dir(self, user_id: str, create: bool = False) -> Path:
        user_dir = self.global_base_dir / self._safe_user_dir(user_id)
        if create:
            user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    @staticmethod
    def _natural_key(path: Path) -> List[Tuple[int, Any]]:
        """自然排序：1.jpg < 2.jpg < 10.jpg，避免字符串排序造成的页序错乱。"""
        key: List[Tuple[int, Any]] = []
        for token in _NATURAL_SPLIT.split(str(path)):
            if token.isdigit():
                key.append((1, int(token)))
            else:
                key.append((0, token.lower()))
        return key

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            resolved = path.resolve()
            parent_resolved = parent.resolve()
            return resolved != parent_resolved and parent_resolved in resolved.parents
        except (OSError, RuntimeError):
            return False

    def _iter_image_files(self, image_dir: Path) -> List[Path]:
        if not image_dir.is_dir():
            return []
        try:
            files = [
                Path(path)
                for path in image_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            ]
        except OSError as exc:
            logger.warning(f"扫描图片目录失败 {image_dir}: {exc}")
            return []
        return sorted(files, key=self._natural_key)

    def _check_access(self, event: AstrMessageEvent) -> Optional[str]:
        if not self.allowed_groups:
            return None
        group_id = event.get_group_id()
        if not group_id:
            return None
        if str(group_id) in self.allowed_groups:
            return None
        return "本群未授权使用该插件"

    def _reserve_job(self, event: AstrMessageEvent) -> Optional[str]:
        """在事件循环内同步完成的“占位”，避免并发请求绕过限流。"""
        user_dir = self._safe_user_dir(event.get_sender_id())
        now = time.monotonic()
        last = self._last_request.get(user_dir, 0.0)
        if self.cooldown_seconds and now - last < self.cooldown_seconds:
            wait = int(self.cooldown_seconds - (now - last)) + 1
            return f"操作太频繁，请等待 {wait} 秒后再试"
        if self._user_jobs.get(user_dir, 0) >= 1:
            return "你还有下载任务在进行，请等待完成后再试"
        self._last_request[user_dir] = now
        self._user_jobs[user_dir] = self._user_jobs.get(user_dir, 0) + 1
        return None

    def _release_job(self, event: AstrMessageEvent):
        user_dir = self._safe_user_dir(event.get_sender_id())
        self._user_jobs[user_dir] = max(0, self._user_jobs.get(user_dir, 1) - 1)

    async def _safe_send(self, event: AstrMessageEvent, text: str):
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:
            logger.error(f"发送消息失败: {exc}")

    # ------------------------------------------------------------------
    # jmcomic 选项
    # ------------------------------------------------------------------
    async def _get_option(self, user_id: str = None) -> Optional["JmOption"]:
        if self._need_warmup and (self._warmup_task is None or self._warmup_task.done()):
            self._warmup_task = self._start_task(self._warmup)
        try:
            if self.option_file and Path(self.option_file).exists():
                option = await asyncio.to_thread(create_option_by_file, self.option_file)
            else:
                option = await asyncio.to_thread(JmOption.default)
            option.dir_rule.base_dir = str(self._user_base_dir(user_id, create=True))
            try:
                rule_dsl = str(getattr(option.dir_rule, "rule_dsl", "") or "")
                if rule_dsl and "Bd" not in rule_dsl and not self._warned_no_bd:
                    self._warned_no_bd = True
                    logger.warning(
                        "自定义 option_file 的 dir_rule 未包含 Bd（基础目录规则），"
                        f"下载文件可能落在 {option.dir_rule.base_dir} 之外，插件将跳过自动清理"
                    )
            except Exception:
                pass
            return option
        except Exception as exc:
            logger.error(f"创建 JmOption 失败: {exc}")
            return None

    async def _run_sync(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    async def _safe_call(self, func, *args, **kwargs):
        try:
            return await self._run_sync(func, *args, **kwargs)
        except (DownloadCancelledException, _JobCancelled):
            raise
        except MissingAlbumPhotoException as exc:
            raise RuntimeError(f"本子/章节不存在: {exc}") from exc
        except RequestRetryAllFailException as exc:
            raise RuntimeError(f"请求重试失败，请稍后重试: {exc}") from exc
        except JmcomicException as exc:
            raise RuntimeError(f"jmcomic 错误: {exc}") from exc
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise RuntimeError(f"未知错误: {exc}") from exc

    async def _safe_call_with_timeout(self, func, timeout: int, *args, **kwargs):
        try:
            return await asyncio.wait_for(self._run_sync(func, *args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"操作超时（{timeout}秒）") from None
        except Exception as exc:
            logger.error(traceback.format_exc())
            raise exc

    # ------------------------------------------------------------------
    # 图片目录与 PDF
    # ------------------------------------------------------------------
    def _select_photos(self, album, chapter_range: Optional[Tuple[int, int]]) -> List[Any]:
        total = len(album)
        photos = [album[index] for index in range(total)]
        if chapter_range:
            start, end = chapter_range
            photos = photos[max(0, start - 1):min(total, end)]
        return photos

    def _collect_album_images(self, option: "JmOption", photos: List[Any]) -> Tuple[List[Path], List[Path]]:
        """按章节顺序收集图片，目录由 jmcomic 的 dir_rule 精确推导。"""
        files: List[Path] = []
        dirs: List[Path] = []
        seen_dirs = set()
        seen_files = set()
        for photo in photos:
            try:
                photo_dir = Path(option.decide_image_save_dir(photo))
            except Exception as exc:
                logger.warning(f"推导章节目录失败: {exc}")
                continue
            try:
                resolved_dir = photo_dir.resolve()
            except OSError:
                resolved_dir = photo_dir
            if resolved_dir in seen_dirs:
                continue
            seen_dirs.add(resolved_dir)
            dirs.append(resolved_dir)
            for file in self._iter_image_files(resolved_dir):
                try:
                    resolved_file = file.resolve()
                except OSError:
                    resolved_file = file
                if resolved_file in seen_files:
                    continue
                seen_files.add(resolved_file)
                files.append(resolved_file)
        return files, dirs

    def _fallback_album_dir(self, user_id: str, album_id: str) -> Optional[Path]:
        """兜底：只认本子 ID 命中且目录唯一的候选，绝不“按最近修改”跨本子取图。"""
        root = self._user_base_dir(user_id)
        if not root.is_dir():
            return None
        try:
            candidates = {
                path.resolve()
                for path in root.rglob(f"*{album_id}*")
                if path.is_dir() and path != root and self._iter_image_files(path)
            }
        except OSError as exc:
            logger.warning(f"兜底扫描目录失败: {exc}")
            return None
        if len(candidates) != 1:
            return None
        folder = candidates.pop()
        logger.warning(f"使用兜底目录（仅 ID 命中且唯一）: {folder}")
        return folder

    def _common_album_root(self, dirs: List[Path], user_dir: Path) -> Optional[Path]:
        if not dirs:
            return None
        try:
            common = Path(os.path.commonpath([str(path) for path in dirs]))
        except ValueError:
            return None
        if not self._is_within(common, user_dir):
            return None
        return common

    @staticmethod
    def _pdf_key(album_id: str, chapter_range: Optional[Tuple[int, int]], quality: int, max_size: int) -> str:
        token = re.sub(r"[^0-9A-Za-z_-]", "_", str(album_id))[:64] or "album"
        suffix = f"_{chapter_range[0]}-{chapter_range[1]}" if chapter_range else ""
        return f"{token}{suffix}_q{quality}_s{max_size}"

    def _pdf_output_path(
        self,
        user_id: str,
        album_id: str,
        chapter_range: Optional[Tuple[int, int]],
        quality: int,
        max_size: int,
    ) -> Tuple[Path, bool]:
        key = self._pdf_key(album_id, chapter_range, quality, max_size)
        if self.pdf_cache_max > 0:
            return self.pdf_cache_dir / f"{key}.pdf", True
        return self._user_base_dir(user_id, create=True) / "pdfs" / f"{key}.pdf", False

    def _prune_pdf_cache(self):
        if self.pdf_cache_max <= 0:
            return
        try:
            files = sorted(
                (path for path in self.pdf_cache_dir.glob("*.pdf") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError as exc:
            logger.warning(f"清理 PDF 缓存失败: {exc}")
            return
        for old in files[self.pdf_cache_max:]:
            try:
                old.unlink()
                logger.info(f"已清理 PDF 缓存: {old.name}")
            except OSError as exc:
                logger.warning(f"删除 PDF 缓存失败 {old}: {exc}")

    @staticmethod
    def _flatten_for_pdf(image):
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = PILImage.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        if image.mode != "RGB":
            return image.convert("RGB")
        return image

    def _render_pdf(
        self,
        image_files: List[Path],
        output_pdf: Path,
        quality: int,
        max_size: int,
    ) -> Tuple[bool, str]:
        """同步执行，调用方必须放到线程里跑。"""
        if not PDF_AVAILABLE:
            return False, "PDF 库未安装"
        workdir: Optional[str] = None
        try:
            output_pdf.parent.mkdir(parents=True, exist_ok=True)

            def make_workdir() -> str:
                # 临时文件放在输出目录旁边：同盘、无跨卷问题，也不受系统临时目录权限影响。
                # 不用 mkdtemp：某些 Windows 受限环境里它建出来的目录当场不可写。
                for _ in range(8):
                    candidate = output_pdf.parent / f".jm_pdf_{os.getpid()}_{time.time_ns()}"
                    try:
                        candidate.mkdir()
                    except FileExistsError:
                        continue
                    except OSError:
                        break
                    return str(candidate)
                return tempfile.mkdtemp(prefix="jm_pdf_")

            paths: List[str] = []
            lossless = max_size <= 0 and quality >= 95
            if lossless:
                # 原图直通：JPEG 不重新编码，PDF 体积可控且无二次损失
                for file in image_files:
                    if file.suffix.lower() in (".jpg", ".jpeg"):
                        paths.append(str(file))
                    else:
                        if workdir is None:
                            workdir = make_workdir()
                        target = Path(workdir) / f"{len(paths):05d}.jpg"
                        with PILImage.open(file) as image:
                            image.load()
                            self._flatten_for_pdf(image).save(target, "JPEG", quality=95)
                        paths.append(str(target))
            else:
                workdir = make_workdir()
                resample = getattr(PILImage, "Resampling", PILImage).LANCZOS
                for index, file in enumerate(image_files):
                    with PILImage.open(file) as image:
                        image.load()
                        work = ImageOps.exif_transpose(image)
                        work = self._flatten_for_pdf(work)
                        if max_size > 0:
                            work.thumbnail((max_size, max_size), resample)
                        target = Path(workdir) / f"{index:05d}.jpg"
                        work.save(target, "JPEG", quality=quality, optimize=True)
                        paths.append(str(target))

            with open(output_pdf, "wb") as stream:
                # img2pdf 的 *images 是可变参数，传列表会被当成单个对象而产出空 PDF
                img2pdf.convert(*paths, outputstream=stream)
            if output_pdf.stat().st_size < 200:
                return False, f"PDF 内容异常（{output_pdf.stat().st_size} 字节）"
            return True, ""
        except Exception as exc:
            logger.error(f"PDF 生成失败: {exc}\n{traceback.format_exc()}")
            return False, str(exc)
        finally:
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    async def _build_pdf(
        self,
        user_id: str,
        album_id: str,
        chapter_range: Optional[Tuple[int, int]],
        quality: int,
        max_size: int,
        image_files: List[Path],
    ) -> Optional[Path]:
        target, cacheable = self._pdf_output_path(user_id, album_id, chapter_range, quality, max_size)
        if cacheable and target.exists() and target.stat().st_size > 0:
            logger.info(f"命中 PDF 缓存: {target.name}")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target.with_name(target.name + ".part")
        ok, error = await asyncio.to_thread(self._render_pdf, image_files, temp_target, quality, max_size)
        if not ok:
            temp_target.unlink(missing_ok=True)
            raise RuntimeError(f"PDF 生成失败: {error}")
        os.replace(temp_target, target)
        if cacheable:
            self._prune_pdf_cache()
        return target

    # ------------------------------------------------------------------
    # 打包
    # ------------------------------------------------------------------
    def _zip_path(self, user_dir: Path, album_id: str, chapter_range: Optional[Tuple[int, int]]) -> Path:
        token = re.sub(r"[^0-9A-Za-z_-]", "_", str(album_id))[:64] or "album"
        suffix = f"_{chapter_range[0]}-{chapter_range[1]}" if chapter_range else ""
        return user_dir / f"{token}{suffix}.zip"

    def _write_zip(self, base: Path, dirs: List[Path], zip_path: Path) -> int:
        count = 0
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        temp_zip = zip_path.with_name(zip_path.name + ".part")
        try:
            with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for folder in dirs:
                    for file in self._iter_image_files(folder):
                        try:
                            arcname = str(file.relative_to(base))
                        except ValueError:
                            arcname = file.name
                        archive.write(file, arcname)
                        count += 1
            if count == 0:
                temp_zip.unlink(missing_ok=True)
                return 0
            os.replace(temp_zip, zip_path)
            return count
        finally:
            temp_zip.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 下载任务
    # ------------------------------------------------------------------
    def _create_range_downloader(self, start: int, end: int):
        class RangeDownloader(JmDownloader):
            def do_filter(self, detail):
                if detail.is_album():
                    album_len = len(detail)
                    begin = max(0, start - 1)
                    finish = min(album_len, end)
                    if begin >= finish:
                        return []
                    return detail[begin:finish]
                return detail

        return RangeDownloader

    def _create_downloader(self, chapter_range: Optional[Tuple[int, int]], reporter: "_ProgressReporter"):
        base_class = (
            self._create_range_downloader(*chapter_range) if chapter_range else JmDownloader
        )

        class ProgressDownloader(base_class):
            def before_album(self, album):
                parent = getattr(super(), "before_album", None)
                if parent is not None:
                    parent(album)
                reporter.start_album(album)

            def after_photo(self, photo):
                parent = getattr(super(), "after_photo", None)
                if parent is not None:
                    parent(photo)
                reporter.on_photo(photo)

            def after_image(self, image, img_save_path):
                parent = getattr(super(), "after_image", None)
                if parent is not None:
                    parent(image, img_save_path)
                reporter.on_image()

        return ProgressDownloader

    def _free_space_mb(self) -> Optional[float]:
        try:
            return shutil.disk_usage(self.global_base_dir).free / (1024 * 1024)
        except OSError:
            return None

    def _over_limit(self, path: Path) -> bool:
        if self.max_file_size_mb <= 0:
            return False
        try:
            return path.stat().st_size > self.max_file_size_mb * 1024 * 1024
        except OSError:
            return False

    async def _send_file(self, event: AstrMessageEvent, path: Path, caption: str) -> bool:
        size_mb = path.stat().st_size / (1024 * 1024)
        if self._over_limit(path):
            await self._safe_send(
                event,
                f"{caption}\n文件大小 {size_mb:.1f} MB，超过发送上限 "
                f"{self.max_file_size_mb} MB。请降低清晰度后重试，例如："
                f"在下载命令后加 --quality=60 --max-size=1600",
            )
            return False
        try:
            await event.send(
                event.chain_result([Plain(caption), File(file=str(path), name=path.name)])
            )
            return True
        except Exception as exc:
            logger.error(f"发送文件失败 {path}: {exc}")
            await self._safe_send(event, f"发送文件失败: {exc}")
            return False

    async def _download_album_task(
        self,
        event: AstrMessageEvent,
        album_id: str,
        pack: bool,
        chapter_range: Optional[Tuple[int, int]],
        quality: int,
        max_size: int,
    ):
        user_id = event.get_sender_id()
        user_key = self._safe_user_dir(user_id)
        if not pack and self.pdf_cache_max > 0:
            # PDF 缓存为全局共享路径。跨用户同参数请求也必须串行，避免竞争写同一个 .part 文件。
            cache_key = self._pdf_key(album_id, chapter_range, quality, max_size)
            lock_key = f"pdf-cache:{cache_key}"
        else:
            lock_key = f"{user_key}:{album_id}"
        lock = self._get_album_lock(lock_key)
        cleanup_targets: List[Path] = []
        job: Dict[str, Any] = {
            "album_id": album_id,
            "cancel": threading.Event(),
            "control": None,
            "started": time.time(),
            "stage": "排队中",
        }
        self._active_jobs[user_key] = job
        try:
            if user_key in self._cancel_requests:
                self._cancel_requests.discard(user_key)
                await self._safe_send(event, f"已取消排队中的任务 {album_id}")
                return

            if not pack:
                cached, cacheable = self._pdf_output_path(user_id, album_id, chapter_range, quality, max_size)
                if cacheable and cached.exists() and cached.stat().st_size > 0:
                    await self._send_file(event, cached, f"本子 {album_id} 下载完成（缓存命中，质量={quality}）：")
                    self._mark_album_sent(album_id)
                    return

            free_mb = self._free_space_mb()
            if self.min_free_disk_mb and free_mb is not None and free_mb < self.min_free_disk_mb:
                await self._safe_send(
                    event,
                    f"磁盘剩余空间不足（{free_mb:.0f} MB < {self.min_free_disk_mb} MB），已取消下载",
                )
                return

            if self._running_downloads >= self.max_concurrent:
                await self._safe_send(
                    event,
                    f"当前有 {self._running_downloads} 个任务在进行，已加入队列，请稍候…",
                )

            async with lock:
                if not pack:
                    cached, cacheable = self._pdf_output_path(
                        user_id, album_id, chapter_range, quality, max_size
                    )
                    if cacheable and cached.exists() and cached.stat().st_size > 0:
                        await self._send_file(
                            event, cached, f"本子 {album_id} 下载完成（缓存命中，质量={quality}）："
                        )
                        self._mark_album_sent(album_id)
                        return

                async with self._get_sem():
                    if job["cancel"].is_set():
                        raise _JobCancelled()
                    self._running_downloads += 1
                    try:
                        cleanup_targets = await self._download_and_send(
                            event, album_id, pack, chapter_range, quality, max_size, job
                        )
                    finally:
                        self._running_downloads -= 1

            if job["cancel"].is_set():
                await self._safe_send(
                    event, f"已停止下载 {album_id}（已下载的图片保留在磁盘，可重新发起继续）"
                )
                return

            if cleanup_targets:
                if self.cleanup_mode == "after_send":
                    self._start_task(self._delete_after_send, user_id, cleanup_targets)
                else:
                    self._start_task(self._cleanup_old_albums, user_id)
        except (DownloadCancelledException, _JobCancelled):
            await self._safe_send(
                event, f"已停止下载 {album_id}（已下载的图片保留在磁盘，可重新发起继续）"
            )
        except asyncio.CancelledError:
            logger.info(f"下载任务被取消: {album_id}")
            raise
        except Exception as exc:
            logger.error(f"下载任务异常: {traceback.format_exc()}")
            await self._safe_send(event, f"下载失败: {exc}")
        finally:
            self._active_jobs.pop(user_key, None)
            self._cancel_requests.discard(user_key)
            self._release_job(event)

    async def _download_and_send(
        self,
        event: AstrMessageEvent,
        album_id: str,
        pack: bool,
        chapter_range: Optional[Tuple[int, int]],
        quality: int,
        max_size: int,
        job: Optional[Dict[str, Any]] = None,
    ) -> List[Path]:
        user_id = event.get_sender_id()
        job = job or {}
        cancel_event: threading.Event = job.get("cancel") or threading.Event()
        reporter = _ProgressReporter(
            self, event, album_id, chapter_range, self.progress_interval
        )
        option = await self._get_option(user_id)
        if option is None:
            await self._safe_send(event, "无法创建下载配置，请检查 option_file 配置")
            return []

        control = None
        if DownloadControl is not None:
            control = DownloadControl()
            job["control"] = control
            if cancel_event.is_set():
                control.cancel("用户主动停止")

        downloader_class = self._create_downloader(chapter_range, reporter)
        download_kwargs: Dict[str, Any] = {"downloader": downloader_class}
        if control is not None and _DOWNLOAD_ALBUM_HAS_CONTROL:
            download_kwargs["control"] = control

        job["stage"] = "下载图片"
        result = await self._safe_call(download_album, album_id, option, **download_kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            album = result[0]
        else:
            album = result
        if album is None or len(album) == 0:
            await self._safe_send(event, "本子无章节，无法确定图片目录")
            return []
        if cancel_event.is_set():
            raise _JobCancelled()

        reporter.finish(f"图片下载完成（{reporter.done_images} 页），正在整理文件…")
        photos = self._select_photos(album, chapter_range)
        image_files, dirs = await asyncio.to_thread(self._collect_album_images, option, photos)
        if not image_files:
            fallback = await asyncio.to_thread(self._fallback_album_dir, user_id, album_id)
            if fallback is not None:
                image_files = await asyncio.to_thread(self._iter_image_files, fallback)
                dirs = [fallback]
        if not image_files:
            await self._safe_send(event, "下载完成但没有找到图片，请开启 enable_jm_log 后检查 dir_rule")
            return []
        if cancel_event.is_set():
            raise _JobCancelled()

        user_dir = self._user_base_dir(user_id, create=True)
        album_root = self._common_album_root(dirs, user_dir)
        logger.info(
            f"本子 {album_id}: 章节 {len(photos)}，图片 {len(image_files)}，目录 {album_root}"
        )

        cleanup_targets: List[Path] = []
        if pack:
            reporter.finish(f"正在打包 ZIP（{len(image_files)} 张图片）…")
            if cancel_event.is_set():
                raise _JobCancelled()
            zip_base = (album_root or dirs[0]).parent
            zip_dirs = [album_root] if album_root else dirs
            zip_path = self._zip_path(user_dir, album_id, chapter_range)
            count = await asyncio.to_thread(self._write_zip, zip_base, zip_dirs, zip_path)
            if count == 0:
                await self._safe_send(event, "下载完成但文件夹为空")
                return []
            if cancel_event.is_set():
                raise _JobCancelled()
            if await self._send_file(event, zip_path, f"本子 {album_id} 打包完成（{count} 张图片）："):
                self._mark_album_sent(album_id)
                cleanup_targets.append(zip_path)
                if album_root:
                    cleanup_targets.append(album_root)
            return cleanup_targets

        cached, cacheable = self._pdf_output_path(user_id, album_id, chapter_range, quality, max_size)
        reporter.finish(f"正在生成 PDF（{len(image_files)} 张图片，质量={quality}）…")
        if cancel_event.is_set():
            raise _JobCancelled()
        pdf_path = await self._build_pdf(
            user_id, album_id, chapter_range, quality, max_size, image_files
        )
        if pdf_path is None:
            await self._safe_send(event, "PDF 生成失败，请查看日志")
            return []
        reporter.finish("PDF 已生成，正在发送…")

        sent_quality, sent_size = quality, max_size
        if self._over_limit(pdf_path):
            fallback_quality = max(40, quality - 30)
            fallback_size = max_size or 1600
            if (fallback_quality, fallback_size) != (quality, max_size):
                logger.info(
                    f"PDF 超过 {self.max_file_size_mb} MB，尝试降级: "
                    f"quality={fallback_quality}, max-size={fallback_size}"
                )
                try:
                    alt_path = await self._build_pdf(
                        user_id,
                        album_id,
                        chapter_range,
                        fallback_quality,
                        fallback_size,
                        image_files,
                    )
                except Exception as exc:
                    logger.warning(f"降级生成 PDF 失败: {exc}")
                    alt_path = None
                if alt_path is not None and not self._over_limit(alt_path):
                    pdf_path = alt_path
                    sent_quality, sent_size = fallback_quality, fallback_size

        caption = (
            f"本子 {album_id} 下载完成，已转换为 PDF"
            f"（质量={sent_quality}{'，已自动降级' if sent_quality != quality else ''}）："
        )
        if cancel_event.is_set():
            raise _JobCancelled()
        if await self._send_file(event, pdf_path, caption):
            self._mark_album_sent(album_id)
            if not cacheable:
                cleanup_targets.append(pdf_path)
            cleanup_targets.extend(self._cleanup_album_targets(album_root, dirs, user_dir))
        return cleanup_targets

    def _cleanup_album_targets(
        self, album_root: Optional[Path], dirs: List[Path], user_dir: Path
    ) -> List[Path]:
        if album_root is not None and self._is_within(album_root, user_dir):
            return [album_root]
        return [path for path in dirs if self._is_within(path, user_dir)]

    async def _delete_after_send(self, user_id: str, targets: List[Path]):
        if self.after_send_delay > 0:
            await asyncio.sleep(self.after_send_delay)
        user_dir = self._user_base_dir(user_id)
        await asyncio.to_thread(self._delete_paths_safely, user_dir, targets)

    def _delete_paths_safely(self, user_dir: Path, targets: List[Path]):
        for target in dict.fromkeys(targets):
            if not self._is_within(target, user_dir):
                logger.error(f"拒绝清理用户目录以外的路径: {target}")
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                logger.info(f"已清理: {target}")
            except Exception as exc:
                logger.error(f"清理失败 {target}: {exc}")

    async def _cleanup_old_albums(self, user_id: str):
        if self.cleanup_mode != "count" or self.max_albums <= 0:
            return
        user_dir = self._user_base_dir(user_id)
        if not user_dir.is_dir():
            return
        exclude_dirs = {"pdfs", "cache", "pica"}
        try:
            album_dirs = [
                path
                for path in user_dir.iterdir()
                if path.is_dir() and path.name not in exclude_dirs
            ]
        except OSError as exc:
            logger.error(f"读取用户下载目录失败 {user_dir}: {exc}")
            return

        # 哔咔的本子放在 <用户>/pica/ 下，不能把整个 pica 目录当成一本算
        pica_root = user_dir / "pica"
        if pica_root.is_dir():
            try:
                album_dirs.extend(path for path in pica_root.iterdir() if path.is_dir())
            except OSError as exc:
                logger.warning(f"读取哔咔下载目录失败 {pica_root}: {exc}")
        album_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        targets = list(album_dirs[self.max_albums:])

        zip_files = sorted(
            (path for path in user_dir.glob("*.zip") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        targets.extend(zip_files[self.max_albums:])

        pdf_dir = user_dir / "pdfs"
        if pdf_dir.is_dir():
            pdf_files = sorted(
                (path for path in pdf_dir.glob("*.pdf") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            targets.extend(pdf_files[self.max_albums:])

        if targets:
            await asyncio.to_thread(self._delete_paths_safely, user_dir, targets)

    def _cleanup_expired(self, days: int) -> int:
        """删除所有用户目录下超过 N 天未修改的文件/文件夹，返回清理数量。"""
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        removed = 0
        try:
            user_dirs = [path for path in self.global_base_dir.iterdir() if path.is_dir()]
        except OSError as exc:
            logger.error(f"读取下载根目录失败 {self.global_base_dir}: {exc}")
            return 0

        for user_dir in user_dirs:
            if user_dir.name in {"cache", "covers"}:  # 缓存/封面由 pdf_cache_max、cover_keep_days 单独管理
                continue
            try:
                entries = list(user_dir.iterdir())
            except OSError as exc:
                logger.warning(f"读取用户目录失败 {user_dir}: {exc}")
                continue
            for target in entries:
                try:
                    if target.name == "cache":
                        continue
                    if target.name == "pica" and target.is_dir():
                        # 哔咔按单本清理：每个本子目录单独看 mtime
                        for album in target.iterdir():
                            try:
                                if album.stat().st_mtime >= cutoff:
                                    continue
                                if album.is_dir():
                                    shutil.rmtree(album)
                                else:
                                    album.unlink()
                                removed += 1
                            except OSError as exc:
                                logger.warning(f"清理过期哔咔文件失败 {album}: {exc}")
                        continue
                    if target.name == "pdfs" and target.is_dir():
                        for pdf in target.glob("*.pdf"):
                            if pdf.stat().st_mtime < cutoff:
                                pdf.unlink()
                                removed += 1
                        continue
                    if target.stat().st_mtime >= cutoff:
                        continue
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning(f"清理过期文件失败 {target}: {exc}")
        if removed:
            logger.info(f"已清理 {removed} 个超过 {days} 天的文件/目录")
        return removed

    # ------------------------------------------------------------------
    # 搜索 / 榜单 / 详情
    # ------------------------------------------------------------------
    async def _do_search(self, event: AstrMessageEvent, keyword: str, page: int):
        try:
            option = await self._get_option(event.get_sender_id())
            if option is None:
                await self._safe_send(event, "无法创建下载配置")
                return
            client = option.build_jm_client()
            search_kwargs = {
                "search_query": keyword,
                "page": page,
                "main_tag": 0,
                "order_by": JmMagicConstants.ORDER_BY_LATEST,
                "time": JmMagicConstants.TIME_ALL,
                "category": JmMagicConstants.CATEGORY_ALL,
                "sub_category": None,
            }
            search_page = await self._safe_call_with_timeout(client.search, 60, **search_kwargs)
            content = search_page.content if hasattr(search_page, "content") else list(search_page or [])
            if not content:
                await self._safe_send(event, "没有找到相关本子")
                return
            page_count = getattr(search_page, "page_count", 1)
            lines = [f"搜索「{keyword}」结果（第{page}/{page_count}页）："]
            for index, (album_id, info) in enumerate(content[:10], 1):
                title = info.get("name", "未知标题") if isinstance(info, dict) else str(info)
                lines.append(f"{index}. ID: {album_id} | {title[:60]}")
            lines.append(f"共{getattr(search_page, 'total', len(content))}条")
            await self._safe_send(event, "\n".join(lines))
        except (asyncio.TimeoutError, TimeoutError):
            await self._safe_send(event, "搜索超时，请稍后重试")
        except Exception as exc:
            logger.error(traceback.format_exc())
            await self._safe_send(event, f"搜索失败: {exc}")

    async def _do_ranking(self, event: AstrMessageEvent, rank_type: str, page: int):
        try:
            option = await self._get_option(event.get_sender_id())
            if option is None:
                await self._safe_send(event, "无法创建下载配置")
                return
            client = option.build_jm_client()
            if rank_type == "month":
                result = await self._safe_call_with_timeout(client.month_ranking, 60, page=page)
            elif rank_type == "week":
                result = await self._safe_call_with_timeout(client.week_ranking, 60, page=page)
            else:
                result = await self._safe_call_with_timeout(client.day_ranking, 60, page=page)
            content = result.content if hasattr(result, "content") else list(result or [])
            if not content:
                await self._safe_send(event, "暂无数据")
                return
            lines = [f"{rank_type}榜 第{page}页："]
            for index, (album_id, info) in enumerate(content[:10], 1):
                title = info.get("name", "未知标题") if isinstance(info, dict) else str(info)
                lines.append(f"{index}. ID: {album_id} | {title[:60]}")
            lines.append(f"共{len(content)}条")
            await self._safe_send(event, "\n".join(lines))
        except (asyncio.TimeoutError, TimeoutError):
            await self._safe_send(event, "获取排行榜超时，请稍后重试")
        except Exception as exc:
            logger.error(traceback.format_exc())
            await self._safe_send(event, f"获取排行榜失败: {exc}")

    async def _do_detail(self, event: AstrMessageEvent, album_id: str):
        try:
            option = await self._get_option(event.get_sender_id())
            if option is None:
                await self._safe_send(event, "无法创建下载配置")
                return
            client = option.build_jm_client()
            album: "JmAlbumDetail" = await self._safe_call(client.get_album_detail, album_id)

            lines = [
                f"标题：{album.title}",
                f"作者：{album.author}",
                f"收藏数：{album.likes}",
                f"章节数：{len(album)}",
            ]
            lines.append(f"标签：{'、'.join(album.tags) if album.tags else '无'}")

            if len(album) > 0:
                lines.append("章节列表：")
                for index, photo in enumerate(album):
                    if index >= 10:
                        lines.append(f"  ... 还有 {len(album) - 10} 个章节")
                        break
                    lines.append(f"  {index + 1}. ID: {photo.photo_id} | {photo.name}")
            else:
                lines.append("该本子暂无章节")

            message_chain = [Plain("\n".join(lines))]
            try:
                cover_filename = f"cover_{album_id}_{int(time.time())}.jpg"
                cover_path = self.cover_dir / cover_filename
                await self._run_sync(client.download_album_cover, album_id, str(cover_path))
                if os.name != "nt" and cover_path.exists():
                    cover_path.chmod(0o644)
                message_chain.append(MsgImage.fromFileSystem(str(cover_path)))
            except Exception as exc:
                logger.warning(f"下载封面失败: {exc}")

            await event.send(event.chain_result(message_chain))
        except Exception as exc:
            logger.error(traceback.format_exc())
            await self._safe_send(event, f"获取详情失败: {exc}")

    # ------------------------------------------------------------------
    # 随机推荐
    # ------------------------------------------------------------------
    _RAND_PERIODS = {
        "day": "day",
        "today": "day",
        "日": "day",
        "今日": "day",
        "week": "week",
        "周": "week",
        "本周": "week",
        "这周": "week",
        "month": "month",
        "月": "month",
        "本月": "month",
        "这月": "month",
    }
    _RAND_LABELS = {"day": "今日榜", "week": "本周榜", "month": "本月榜"}

    @classmethod
    def _parse_rand_tokens(cls, tokens: List[str]) -> Tuple[str, str]:
        period = "day"
        rest = list(tokens)
        if rest:
            key = rest[0].strip().lower()
            if key in cls._RAND_PERIODS:
                period = cls._RAND_PERIODS[key]
                rest = rest[1:]
        keyword = " ".join(rest).strip()
        if len(keyword) > 40:
            raise ValueError("关键词太长了，控制在 40 个字以内")
        return period, keyword

    async def _do_rand(self, event: AstrMessageEvent, period: str, keyword: str):
        """从榜单（或搜索结果）随机挑一本没发过的，然后发详情。"""
        try:
            option = await self._get_option(event.get_sender_id())
            if option is None:
                await self._safe_send(event, "无法创建下载配置")
                return
            client = option.build_jm_client()
            picked: Optional[str] = None
            for _ in range(3):
                page = random.randint(1, 5)
                if keyword:
                    result = await self._safe_call_with_timeout(
                        client.search,
                        60,
                        search_query=keyword,
                        page=page,
                        main_tag=0,
                        order_by=JmMagicConstants.ORDER_BY_LATEST,
                        time=JmMagicConstants.TIME_ALL,
                        category=JmMagicConstants.CATEGORY_ALL,
                        sub_category=None,
                    )
                elif period == "month":
                    result = await self._safe_call_with_timeout(client.month_ranking, 60, page=page)
                elif period == "week":
                    result = await self._safe_call_with_timeout(client.week_ranking, 60, page=page)
                else:
                    result = await self._safe_call_with_timeout(client.day_ranking, 60, page=page)
                content = result.content if hasattr(result, "content") else list(result or [])
                fresh = [
                    str(item[0])
                    for item in content
                    if str(item[0]).isdigit() and str(item[0]) not in self._sent_albums
                ]
                if fresh:
                    picked = random.choice(fresh)
                    break
            if picked is None:
                await self._safe_send(
                    event, "这批都推荐过了，等会儿再试；也可以换个关键词：/jm rand 关键词"
                )
                return
            label = f"「{keyword}」" if keyword else self._RAND_LABELS.get(period, "今日榜")
            await self._safe_send(event, f"随机推荐（{label}）：本子 {picked}，详情如下")
            await self._do_detail(event, picked)
            self._mark_album_sent(picked)
        except (asyncio.TimeoutError, TimeoutError):
            await self._safe_send(event, "获取随机推荐超时，请稍后重试")
        except Exception as exc:
            logger.error(traceback.format_exc())
            await self._safe_send(event, f"随机推荐失败: {exc}")

    # ------------------------------------------------------------------
    # 定期维护
    # ------------------------------------------------------------------
    async def _maintenance_loop(self):
        while True:
            try:
                if self.cover_keep_days > 0 and self.cover_dir.is_dir():
                    cutoff = datetime.now() - timedelta(days=self.cover_keep_days)
                    for file in self.cover_dir.glob("*.jpg"):
                        try:
                            if datetime.fromtimestamp(file.stat().st_mtime) < cutoff:
                                file.unlink()
                                logger.debug(f"已删除过期封面: {file}")
                        except OSError as exc:
                            logger.debug(f"清理封面 {file} 失败: {exc}")
                if self.auto_cleanup_days > 0:
                    await asyncio.to_thread(self._cleanup_expired, self.auto_cleanup_days)
                if self.pdf_cache_max > 0:
                    await asyncio.to_thread(self._prune_pdf_cache)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"维护任务出错: {exc}")
            await asyncio.sleep(3600)

    # ------------------------------------------------------------------
    # 命令解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_album_command(
        args: List[str], cmd_prefix_len: int
    ) -> Tuple[str, Optional[Tuple[int, int]], Dict[str, str]]:
        if len(args) <= cmd_prefix_len:
            raise ValueError("缺少本子ID，例如：/jm 123")
        album_id = args[cmd_prefix_len]
        if not album_id.isdigit():
            raise ValueError(f"本子ID必须是数字：{album_id}")

        start = end = None
        extra: Dict[str, str] = {}
        index = cmd_prefix_len + 1
        while index < len(args):
            arg = args[index]
            if arg.startswith("--"):
                body = arg[2:]
                if not body:
                    raise ValueError("参数格式错误，应形如 --quality=80")
                if "=" in body:
                    key, value = body.split("=", 1)
                elif index + 1 < len(args) and not args[index + 1].startswith("--"):
                    key, value = body, args[index + 1]
                    index += 1
                else:
                    raise ValueError(f"--{body} 缺少取值")
                key = key.strip().lower()
                if key not in {"quality", "max-size"}:
                    raise ValueError(f"未知参数 --{key}，可用参数：--quality、--max-size")
                if not value.strip():
                    raise ValueError(f"--{key} 缺少取值")
                extra[key] = value.strip()
            elif "-" in arg:
                if start is not None:
                    raise ValueError("重复指定章节范围，只能给一个范围或单个章节")
                parts = arg.split("-")
                if len(parts) == 2 and all(part.isdigit() for part in parts):
                    start, end = int(parts[0]), int(parts[1])
                else:
                    raise ValueError(f"无法识别的范围：{arg}，示例：1-5 或 3")
                if start < 1 or end < start:
                    raise ValueError("范围无效：起始章节需 ≥1，且结束章节不小于起始章节")
            elif arg.isdigit():
                if start is not None:
                    raise ValueError("重复指定章节范围，只能给一个范围或单个章节")
                start = end = int(arg)
                if start < 1:
                    raise ValueError("章节序号必须大于 0")
            else:
                raise ValueError(f"无法识别的参数：{arg}")
            index += 1
        return album_id, ((start, end) if start is not None else None), extra

    @staticmethod
    def _parse_int(value: Any, name: str, minimum: int, maximum: int) -> int:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"--{name} 需要整数，当前：{value}")
        if number < minimum or number > maximum:
            raise ValueError(f"--{name} 需在 {minimum}-{maximum} 之间，当前：{number}")
        return number

    @staticmethod
    def _parse_keyword_args(message_str: str) -> Tuple[str, int]:
        tokens = message_str.strip().split()[1:]
        return JmComicPlugin._parse_keyword_tokens(tokens)

    @staticmethod
    def _parse_ranking_args(message_str: str) -> Tuple[str, int]:
        tokens = message_str.strip().split()[1:]
        return JmComicPlugin._parse_ranking_tokens(tokens)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    _SUBCOMMANDS = {
        "download": "download",
        "下载": "download",
        "下": "download",
        "dl": "download",
        "get": "download",
        "pdf": "download",
        "zip": "zip",
        "打包": "zip",
        "压缩": "zip",
        "包": "zip",
        "z": "zip",
        "search": "search",
        "搜": "search",
        "搜索": "search",
        "找": "search",
        "find": "search",
        "s": "search",
        "rank": "rank",
        "排行": "rank",
        "榜": "rank",
        "榜单": "rank",
        "top": "rank",
        "ranking": "rank",
        "r": "rank",
        "detail": "detail",
        "详情": "detail",
        "信息": "detail",
        "info": "detail",
        "d": "detail",
        "rand": "rand",
        "roll": "rand",
        "盲盒": "rand",
        "random": "rand",
        "随机": "rand",
        "推荐": "rand",
        "stop": "stop",
        "停止": "stop",
        "取消": "stop",
        "停": "stop",
        "cancel": "stop",
        "clean": "clean",
        "清理": "clean",
        "清除": "clean",
        "clear": "clean",
        "help": "help",
        "帮助": "help",
        "说明": "help",
        "h": "help",
        "?": "help",
    }

    @classmethod
    def _split_subcommand(cls, message_str: str) -> Tuple[str, List[str]]:
        """把 /jm 之后的文本拆成 (子命令, 参数)。兼容平台剥掉命令前缀的情况。"""
        tokens = message_str.strip().split()
        if not tokens:
            return "help", []
        head = tokens[0].lstrip("/#!").lower()
        if head.startswith("jm"):
            tokens = tokens[1:]
        if not tokens:
            return "help", []
        sub = cls._SUBCOMMANDS.get(tokens[0].lower())
        if sub:
            return sub, tokens[1:]
        if tokens[0].isdigit():
            return "download", tokens
        # 既不是子命令也不是 ID，就当成搜索关键词
        return "search", tokens

    @staticmethod
    def _parse_keyword_tokens(tokens: List[str]) -> Tuple[str, int]:
        if not tokens:
            raise ValueError("请提供搜索关键词，例如：/jm s 关键词")
        page = 1
        if len(tokens) >= 2 and tokens[-1].isdigit():
            candidate = int(tokens[-1])
            if 1 <= candidate <= 100:
                page = candidate
                tokens = tokens[:-1]
        keyword = " ".join(tokens).strip()
        if not keyword:
            raise ValueError("请提供搜索关键词，例如：/jm s 关键词")
        return keyword, page

    @staticmethod
    def _parse_ranking_tokens(tokens: List[str]) -> Tuple[str, int]:
        rank_type = "month"
        page = 1
        for token in tokens:
            lowered = token.lower()
            if lowered in {"week", "month", "day"}:
                rank_type = lowered
            elif token.isdigit():
                page = int(token)
            else:
                raise ValueError(f"无法识别的参数：{token}，可用：week、month、day、页码")
        if page < 1 or page > 100:
            raise ValueError("页码需在 1-100 之间")
        return rank_type, page

    async def _accept_download(self, event: AstrMessageEvent, args: List[str], pack: bool):
        if not pack and not PDF_AVAILABLE:
            return [event.plain_result("PDF 库未安装，请先执行：pip install img2pdf Pillow")]
        denied = self._check_access(event)
        if denied:
            return [event.plain_result(denied)]
        try:
            album_id, chapter_range, extra = self._parse_album_command(args, 0)
            if pack and extra:
                raise ValueError("/jm zip 不支持 --quality/--max-size，打包始终使用原图")
            quality = self._parse_int(
                extra.get("quality", self.default_pdf_quality), "quality", 1, 100
            )
            max_size = self._parse_int(extra.get("max-size", 0), "max-size", 0, 10000)
        except ValueError as exc:
            return [event.plain_result(str(exc))]

        rejected = self._reserve_job(event)
        if rejected:
            return [event.plain_result(rejected)]

        action = "打包" if pack else "下载"
        if chapter_range:
            head = f"{action} {album_id} 第{chapter_range[0]}~{chapter_range[1]}章"
        else:
            head = f"{action} {album_id}"
        if not pack:
            head += f"（质量={quality}）"
        self._start_task(
            self._download_album_task, event, album_id, pack, chapter_range, quality, max_size
        )
        return [event.plain_result(f"{head}，正在解析本子…\n发送「/jm stop」可停止")]

    async def _accept_search(self, event: AstrMessageEvent, args: List[str]):
        denied = self._check_access(event)
        if denied:
            return [event.plain_result(denied)]
        try:
            keyword, page = self._parse_keyword_tokens(args)
        except ValueError as exc:
            return [event.plain_result(str(exc))]
        self._start_task(self._do_search, event, keyword, page)
        return []

    async def _accept_ranking(self, event: AstrMessageEvent, args: List[str]):
        denied = self._check_access(event)
        if denied:
            return [event.plain_result(denied)]
        try:
            rank_type, page = self._parse_ranking_tokens(args)
        except ValueError as exc:
            return [event.plain_result(str(exc))]
        self._start_task(self._do_ranking, event, rank_type, page)
        return []

    async def _accept_detail(self, event: AstrMessageEvent, args: List[str]):
        denied = self._check_access(event)
        if denied:
            return [event.plain_result(denied)]
        if not args:
            return [event.plain_result("请提供本子ID，例如：/jm d 123")]
        album_id = args[0]
        if not album_id.isdigit():
            return [event.plain_result(f"本子ID必须是数字：{album_id}")]
        await self._do_detail(event, album_id)
        return []

    async def _accept_rand(self, event: AstrMessageEvent, args: List[str]):
        denied = self._check_access(event)
        if denied:
            return [event.plain_result(denied)]
        try:
            period, keyword = self._parse_rand_tokens(args)
        except ValueError as exc:
            return [event.plain_result(str(exc))]
        self._start_task(self._do_rand, event, period, keyword)
        return []

    async def _accept_stop(self, event: AstrMessageEvent, args: List[str]):
        user_key = self._safe_user_dir(event.get_sender_id())
        job = self._active_jobs.get(user_key)
        if job is None:
            if self._user_jobs.get(user_key, 0) > 0:
                self._cancel_requests.add(user_key)
                return [event.plain_result("已取消排队中的下载任务")]
            return [event.plain_result("当前没有进行中的下载任务")]
        job["cancel"].set()
        control = job.get("control")
        if control is not None:
            try:
                control.cancel("用户主动停止")
            except Exception as exc:  # pragma: no cover
                logger.warning(f"取消下载失败: {exc}")
        return [
            event.plain_result(
                f"已请求停止本子 {job.get('album_id')}，正在中断…（已下载的图片会保留）"
            )
        ]

    async def _accept_clean(self, event: AstrMessageEvent, args: List[str]):
        denied = self._check_access(event)
        if denied:
            return [event.plain_result(denied)]
        days = self.auto_cleanup_days or 7
        removed = await asyncio.to_thread(self._cleanup_expired, days)
        return [event.plain_result(f"已清理 {removed} 个超过 {days} 天未使用的文件/目录")]

    @staticmethod
    def _help_text() -> str:
        return (
            "【禁漫 / JMComic】按常用程度排：\n"
            "\n"
            "/jm 123              下载本子 123，自动转 PDF\n"
            "/jm rand             随机推荐一本（发过的跳过）\n"
            "/jm 关键词            直接搜索，等于 /jm s\n"
            "/jm 123 1-5          只下第 1~5 章\n"
            "\n"
            "可选参数：\n"
            "  --quality=60       清晰度 1-100，默认 85，越小文件越小\n"
            "  --max-size=1600    图片最长边，0 = 不缩放\n"
            "/jm z 123            打包成 ZIP\n"
            "/jm stop             停止下载\n"
            "/jm r week           排行榜：day / week / month\n"
            "/jm d 123            只看详情，不下载\n"
            "/jm clean            立即清理过期文件\n"
            "/jm help             这条说明\n"
            "\n"
            "中文子命令也认：下载、搜索、排行、详情、随机、停止、清理、帮助。\n"
            "哔咔漫画用 /pica，需先在配置里填 pica_account / pica_password。\n"
        )

    @filter.command("jm")
    async def command_jm(self, event: AstrMessageEvent):
        sub, args = self._split_subcommand(event.message_str)
        if sub == "download":
            results = await self._accept_download(event, args, pack=False)
        elif sub == "zip":
            results = await self._accept_download(event, args, pack=True)
        elif sub == "search":
            results = await self._accept_search(event, args)
        elif sub == "rank":
            results = await self._accept_ranking(event, args)
        elif sub == "detail":
            results = await self._accept_detail(event, args)
        elif sub == "rand":
            results = await self._accept_rand(event, args)
        elif sub == "stop":
            results = await self._accept_stop(event, args)
        elif sub == "clean":
            results = await self._accept_clean(event, args)
        else:
            results = [event.plain_result(self._help_text())]
        for result in results:
            yield result

    @filter.command("jmz")
    async def command_jmz(self, event: AstrMessageEvent):
        tokens = event.message_str.strip().split()
        if tokens and tokens[0].lstrip("/#!").lower().startswith("jmz"):
            tokens = tokens[1:]
        for result in await self._accept_download(event, tokens, pack=True):
            yield result

    @filter.command("jms")
    async def command_jms(self, event: AstrMessageEvent):
        tokens = event.message_str.strip().split()
        if tokens and tokens[0].lstrip("/#!").lower().startswith("jms"):
            tokens = tokens[1:]
        for result in await self._accept_search(event, tokens):
            yield result

    @filter.command("jmr")
    async def command_jmr(self, event: AstrMessageEvent):
        tokens = event.message_str.strip().split()
        if tokens and tokens[0].lstrip("/#!").lower().startswith("jmr"):
            tokens = tokens[1:]
        for result in await self._accept_ranking(event, tokens):
            yield result

    @filter.command("pica")
    async def command_pica(self, event: AstrMessageEvent):
        for result in await self._accept_pica_entry(event):
            yield result

    @filter.command("bika")
    async def command_bika(self, event: AstrMessageEvent):
        for result in await self._accept_pica_entry(event):
            yield result

    async def terminate(self):
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in list(self._active_jobs.values()):
            control = job.get("control")
            if control is not None:
                try:
                    control.cancel("插件卸载")
                except Exception:
                    pass
        self._active_jobs.clear()
        self._cancel_requests.clear()
        self._album_locks.clear()
        await self._pica_terminate()
        logger.info("禁漫/哔咔插件已卸载")
