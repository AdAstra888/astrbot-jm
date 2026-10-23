# -*- coding: utf-8 -*-
"""PicACG（哔咔）功能混入类。

放在单独的 mixin 里，main.py 只负责把命令转发进来，避免主文件继续膨胀。
所有复用的工具方法（_send_file / _build_pdf / _write_zip / _reserve_job 等）都由
JmComicPlugin 提供，这里直接通过 self 调用。
"""

from __future__ import annotations

import asyncio
import random
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:  # pragma: no cover - 仅用于没有 aiohttp 的极端环境
    from .client import PicaClient, PicaError
except ImportError:  # pragma: no cover
    PicaClient = None  # type: ignore

    class PicaError(Exception):  # type: ignore
        pass


class PicaCancelled(Exception):
    """哔咔任务被用户停止。"""


_PICA_SUBCOMMANDS = {
    "": "download",
    "download": "download",
    "下载": "download",
    "下": "download",
    "dl": "download",
    "down": "download",
    "z": "zip",
    "打包": "zip",
    "压缩": "zip",
    "包": "zip",
    "zip": "zip",
    "pack": "zip",
    "s": "search",
    "搜": "search",
    "找": "search",
    "search": "search",
    "find": "search",
    "搜": "search",
    "搜索": "search",
    "d": "detail",
    "信息": "detail",
    "detail": "detail",
    "info": "detail",
    "详情": "detail",
    "eps": "eps",
    "章": "eps",
    "episodes": "eps",
    "chapters": "eps",
    "章节": "eps",
    "r": "rank",
    "榜": "rank",
    "榜单": "rank",
    "top": "rank",
    "rank": "rank",
    "ranking": "rank",
    "排行": "rank",
    "rand": "rand",
    "random": "rand",
    "随机": "rand",
    "fav": "fav",
    "收藏夹": "fav",
    "favorite": "fav",
    "favourites": "fav",
    "收藏": "fav",
    "stop": "stop",
    "停": "stop",
    "取消": "stop",
    "cancel": "stop",
    "停止": "stop",
    "login": "login",
    "状态": "login",
    "检查": "login",
    "status": "login",
    "help": "help",
    "帮助": "help",
    "说明": "help",
    "?": "help",
    "h": "help",
}


class PicaMixin:
    # ------------------------------------------------------------------
    # 配置与客户端
    # ------------------------------------------------------------------
    def _pica_conf(self, key: str, default: Any = None) -> Any:
        return (getattr(self, "config", None) or {}).get(key, default)

    def _pica_str(self, key: str, default: str = "") -> str:
        return str(self._pica_conf(key, default) or "").strip()

    def _pica_int(self, key: str, default: int, minimum: int) -> int:
        raw = self._pica_conf(key, default)
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _pica_bool(self, key: str, default: bool) -> bool:
        raw = self._pica_conf(key, default)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")

    @property
    def _pica_token_file(self) -> Path:
        return Path(self.pdf_cache_dir).parent / "pica_token.json"

    def _pica_configured(self) -> bool:
        return bool(self._pica_str("pica_account") and self._pica_str("pica_password")) or bool(
            self._pica_str("pica_token")
        )

    def _get_pica_client(self):
        if PicaClient is None:
            return None
        client = self.__dict__.get("_pica_client")
        if client is None:
            client = PicaClient(
                account=self._pica_str("pica_account"),
                password=self._pica_str("pica_password"),
                token=self._pica_str("pica_token"),
                proxy=self._pica_str("pica_proxy"),
                verify_ssl=self._pica_bool("pica_verify_ssl", True),
                token_file=self._pica_token_file,
                api_base=self._pica_str("pica_api_base"),
                allow_http_fallback=self._pica_bool("pica_allow_http_fallback", False),
            )
            self.__dict__["_pica_client"] = client
        return client

    async def _pica_terminate(self):
        client = self.__dict__.get("_pica_client")
        if client is not None:
            try:
                await client.close()
            except Exception:  # pragma: no cover
                pass
            self.__dict__["_pica_client"] = None

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @classmethod
    def _split_pica_subcommand(cls, message_str: str) -> Tuple[str, List[str]]:
        tokens = (message_str or "").strip().split()
        if tokens:
            head = tokens[0].lstrip("/#!").lower()
            if head.startswith("pica") or head.startswith("bika"):
                tokens = tokens[1:]
        if not tokens:
            return "help", []
        sub = _PICA_SUBCOMMANDS.get(tokens[0].lower())
        if sub is None:
            if re.fullmatch(r"[0-9A-Za-z]{6,40}", tokens[0]):
                return "download", tokens
            # 既不是子命令也不是本子 ID，就当成搜索关键词
            return "search", tokens
        return sub, tokens[1:]

    @staticmethod
    def _pica_spec_token(spec: str) -> str:
        token = re.sub(r"[^0-9A-Za-z_-]", "", str(spec or ""))[:64]
        return token or "all"

    @staticmethod
    def _parse_chapter_spec(text: str) -> Optional[List[Tuple[int, int]]]:
        raw = str(text or "").strip().lower().replace(" ", "")
        if raw in ("", "all", "*", "全部"):
            return None
        ranges: List[Tuple[int, int]] = []
        for part in raw.split(","):
            if not part:
                continue
            match = re.fullmatch(r"(\d+)-(\d+)", part)
            if match:
                start, end = int(match.group(1)), int(match.group(2))
                if start < 1 or end < start:
                    raise ValueError(f"章节范围不合法: {part}")
                ranges.append((start, end))
                continue
            if part.isdigit():
                index = int(part)
                if index < 1:
                    raise ValueError(f"章节序号必须大于 0: {part}")
                ranges.append((index, index))
                continue
            raise ValueError(f"无法识别的章节参数: {part}")
        if not ranges:
            raise ValueError("章节参数为空")
        return ranges

    @staticmethod
    def _chapter_spec_label(spec: str) -> str:
        return "全部章节" if spec in ("", "all", "*", "全部") else f"第 {spec} 章"

    @staticmethod
    def _full_range(ranges: Optional[List[Tuple[int, int]]]) -> Optional[Tuple[int, int]]:
        if not ranges:
            return None
        return min(start for start, _ in ranges), max(end for _, end in ranges)

    @staticmethod
    def _select_pica_episodes(
        episodes: List[Dict[str, Any]], ranges: Optional[List[Tuple[int, int]]]
    ) -> List[Tuple[int, Dict[str, Any]]]:
        def order_key(item):
            index, episode = item
            raw = episode.get("order")
            return int(raw) if str(raw).isdigit() else index

        ordered = sorted(enumerate(episodes, 1), key=order_key)
        wanted = None
        if ranges is not None:
            wanted = set()
            for start, end in ranges:
                wanted.update(range(start, end + 1))
        # 序号统一按「排序后的位置」计算，避免接口返回顺序与 order 字段不一致
        selected: List[Tuple[int, Dict[str, Any]]] = []
        for position, (_original_index, episode) in enumerate(ordered, 1):
            if wanted is None or position in wanted:
                selected.append((position, episode))
        return selected

    @classmethod
    def _parse_pica_tokens(cls, tokens: List[str]) -> Tuple[str, str, Dict[str, str]]:
        positional: List[str] = []
        extra: Dict[str, str] = {}
        for token in tokens:
            if token.startswith("--"):
                key, _, value = token[2:].partition("=")
                if key in ("quality", "max-size") and value:
                    extra[key] = value
                    continue
                raise ValueError(f"未知参数: {token}")
            positional.append(token)
        if not positional:
            raise ValueError("用法：/pica <本子ID> [章节，如 1-5 或 1,3,5]")
        comic_id = positional[0].strip()
        if not re.fullmatch(r"[0-9A-Za-z]{6,40}", comic_id):
            raise ValueError(f"哔咔本子 ID 格式不对: {comic_id}")
        spec = positional[1] if len(positional) > 1 else "all"
        if len(positional) > 2:
            raise ValueError("参数太多，只支持一个章节表达式")
        return comic_id, spec, extra

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def _pica_comic_dir(self, user_id: str, comic_id: str, title: str) -> Path:
        user_dir = self._user_base_dir(user_id, create=True)
        from .client import _safe_name

        folder = f"{re.sub(r'[^0-9A-Za-z]', '', comic_id)[:12]}_{_safe_name(title, 50)}"
        return Path(user_dir) / "pica" / folder

    async def _pica_download_task(
        self,
        event: AstrMessageEvent,
        comic_id: str,
        spec: str,
        ranges: Optional[List[Tuple[int, int]]],
        pack: bool,
        quality: int,
        max_size: int,
    ):
        user_id = event.get_sender_id()
        user_key = self._safe_user_dir(user_id)
        album_id = f"pica_{comic_id}"
        range_tuple = self._full_range(ranges)
        lock = self._get_album_lock(f"{user_key}:{album_id}")
        job: Dict[str, Any] = {
            "album_id": album_id,
            "platform": "pica",
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
                cached, cacheable = self._pdf_output_path(
                    user_id, album_id, range_tuple, quality, max_size
                )
                if cacheable and cached.exists() and cached.stat().st_size > 0:
                    await self._send_file(
                        event,
                        cached,
                        f"哔咔本子 {comic_id} 下载完成（缓存命中，质量={quality}）：",
                    )
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
                async with self._get_sem():
                    if job["cancel"].is_set():
                        raise PicaCancelled()
                    self._running_downloads += 1
                    try:
                        cleanup_targets = await self._pica_download_and_send(
                            event, comic_id, spec, ranges, pack, quality, max_size, job
                        )
                    finally:
                        self._running_downloads -= 1
            if job["cancel"].is_set():
                await self._safe_send(
                    event,
                    f"已停止下载 {comic_id}（已下载的图片保留在磁盘，可重新发起继续）",
                )
                return
            if cleanup_targets:
                if getattr(self, "cleanup_mode", "count") == "after_send":
                    self._start_task(self._delete_after_send, user_id, cleanup_targets)
                else:
                    self._start_task(self._cleanup_old_albums, user_id)
        except Exception as exc:
            if isinstance(exc, PicaCancelled):
                await self._safe_send(
                    event,
                    f"已停止下载 {comic_id}（已下载的图片保留在磁盘，可重新发起继续）",
                )
            elif isinstance(exc, asyncio.CancelledError):
                logger.info(f"哔咔下载任务被取消: {comic_id}")
                raise
            else:
                logger.error(f"哔咔下载任务异常: {traceback.format_exc()}")
                await self._safe_send(event, f"哔咔下载失败: {exc}")
        finally:
            self._active_jobs.pop(user_key, None)
            self._cancel_requests.discard(user_key)
            self._release_job(event)

    async def _pica_call(self, func, *args, timeout: int = 90, **kwargs):
        return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)

    async def _pica_download_and_send(
        self,
        event: AstrMessageEvent,
        comic_id: str,
        spec: str,
        ranges: Optional[List[Tuple[int, int]]],
        pack: bool,
        quality: int,
        max_size: int,
        job: Dict[str, Any],
    ) -> List[Path]:
        client = self._get_pica_client()
        if client is None:
            await self._safe_send(event, "哔咔支持未启用：缺少 aiohttp 依赖")
            return []
        cancel_event: threading.Event = job["cancel"]
        user_id = event.get_sender_id()
        album_id = f"pica_{comic_id}"
        range_tuple = self._full_range(ranges)

        job["stage"] = "获取详情"
        comic = await self._pica_call(client.comic, comic_id)
        title = str(comic.get("title") or comic_id)
        await self._safe_send(event, f"哔咔：{title}")

        job["stage"] = "获取章节"
        episodes = await self._pica_call(client.episodes, comic_id)
        selected = self._select_pica_episodes(episodes, ranges)
        if not selected:
            await self._safe_send(
                event, "没有找到指定章节，请用 /pica eps <ID> 查看章节列表"
            )
            return []

        free_mb = self._free_space_mb()
        if self.min_free_disk_mb and free_mb is not None and free_mb < self.min_free_disk_mb:
            await self._safe_send(
                event,
                f"磁盘剩余空间不足（{free_mb:.0f} MB < {self.min_free_disk_mb} MB），已取消下载",
            )
            return []

        comic_dir = self._pica_comic_dir(user_id, comic_id, title)
        comic_dir.mkdir(parents=True, exist_ok=True)
        dirs: List[Path] = []
        image_files: List[Path] = []
        last_progress = 0.0
        concurrency = max(1, min(8, self._pica_int("pica_concurrency", 4, 1)))
        interval = max(3, int(getattr(self, "progress_interval", 8)))
        failed_episodes: List[str] = []

        for position, (index, episode) in enumerate(selected, 1):
            if cancel_event.is_set():
                raise PicaCancelled()
            ep_title = str(episode.get("title") or f"第{index}章")
            job["stage"] = f"章节 {index}"
            await self._safe_send(
                event,
                f"开始下载第 {index} 章 {ep_title}（{position}/{len(selected)}）\n"
                f"发送「/pica stop」可停止",
            )
            try:
                pages = await self._pica_call(
                    client.pages, comic_id, str(episode.get("_id") or "")
                )
            except PicaError as exc:
                # 旧章节可能已下架/需要解锁，单章失败不应毁掉整本
                failed_episodes.append(f"{index}.{ep_title}（{exc}）")
                logger.warning("哔咔章节 %s 获取失败: %s", index, exc)
                continue
            if not pages:
                failed_episodes.append(f"{index}.{ep_title}（无图片）")
                continue
            from .client import _safe_name

            ep_dir = comic_dir / f"{index:03d}_{_safe_name(ep_title, 50)}"
            ep_dir.mkdir(parents=True, exist_ok=True)
            semaphore = asyncio.Semaphore(concurrency)

            async def fetch_one(page_index: int, page: Dict[str, Any], target_dir: Path = ep_dir):
                if cancel_event.is_set():
                    raise PicaCancelled()
                url = client.image_url(page)
                if not url:
                    return
                suffix = Path(url.split("?")[0]).suffix.lower()
                if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    suffix = ".jpg"
                dest = target_dir / f"{page_index:03d}{suffix}"
                if dest.exists() and dest.stat().st_size > 0:
                    return
                async with semaphore:
                    if cancel_event.is_set():
                        raise PicaCancelled()
                    await client.download_image(url, dest)

            await asyncio.gather(
                *(fetch_one(page_index, page) for page_index, page in enumerate(pages, 1))
            )
            dirs.append(ep_dir)
            image_files.extend(await asyncio.to_thread(self._iter_image_files, ep_dir))
            now = time.time()
            if now - last_progress >= interval:
                last_progress = now
                await self._safe_send(
                    event, f"进度：第 {index} 章完成，已收集 {len(image_files)} 张图片"
                )
            if cancel_event.is_set():
                raise PicaCancelled()

        if failed_episodes:
            preview = "、".join(failed_episodes[:5])
            tail = "" if len(failed_episodes) <= 5 else f" 等 {len(failed_episodes)} 章"
            await self._safe_send(
                event, f"以下章节无法下载，已跳过：{preview}{tail}"
            )

        if not image_files:
            await self._safe_send(
                event,
                "没有下载到任何图片。常见原因：章节已下架/需要解锁，或该本子只有付费章节",
            )
            return []

        user_dir = self._user_base_dir(user_id, create=True)
        cleanup_targets: List[Path] = []
        if pack:
            await self._safe_send(event, f"正在打包 ZIP（{len(image_files)} 张图片）…")
            zip_path = self._zip_path(user_dir, album_id, range_tuple)
            count = await asyncio.to_thread(
                self._write_zip, comic_dir.parent, dirs, zip_path
            )
            if count == 0:
                await self._safe_send(event, "打包失败：目录为空")
                return []
            caption = (
                f"哔咔《{title}》（{self._chapter_spec_label(spec)}）打包完成"
                f"（{count} 张图片）："
            )
            if await self._send_file(event, zip_path, caption):
                self._mark_album_sent(album_id)
                cleanup_targets.append(zip_path)
                cleanup_targets.extend(dirs)
            return cleanup_targets

        used_quality = quality
        await self._safe_send(
            event, f"正在生成 PDF（{len(image_files)} 张图片，质量={used_quality}）…"
        )
        pdf_path = await self._build_pdf(
            user_id, album_id, range_tuple, used_quality, max_size, image_files
        )
        if pdf_path is None:
            await self._safe_send(event, "PDF 生成失败")
            return []
        caption = f"哔咔《{title}》（{self._chapter_spec_label(spec)}）："
        sent = await self._send_file(event, pdf_path, caption)
        if not sent and used_quality > 40:
            used_quality = max(40, used_quality - 30)
            retry_size = max_size or 1600
            max_size = retry_size  # 让缓存键与降级后实际参数一致
            await self._safe_send(
                event, f"文件过大，自动降低清晰度到 {used_quality} 后重试…"
            )
            pdf_path = await self._build_pdf(
                user_id, album_id, range_tuple, used_quality, retry_size, image_files
            )
            if pdf_path is not None:
                sent = await self._send_file(
                    event,
                    pdf_path,
                    f"哔咔《{title}》（{self._chapter_spec_label(spec)}，"
                    f"质量={used_quality}，已自动降级）：",
                )
        if sent:
            self._mark_album_sent(album_id)
            cacheable = self._pdf_output_path(
                user_id, album_id, range_tuple, used_quality, max_size
            )[1]
            if not cacheable:
                cleanup_targets.append(pdf_path)
            cleanup_targets.extend(dirs)
        return cleanup_targets

    # ------------------------------------------------------------------
    # 查询命令
    # ------------------------------------------------------------------
    async def _do_pica_search(self, event: AstrMessageEvent, keyword: str, page: int):
        client = self._get_pica_client()
        if client is None:
            return await self._safe_send(event, "哔咔支持未启用")
        try:
            data = await self._pica_call(client.search, keyword, page, timeout=60)
        except Exception as exc:
            return await self._safe_send(event, f"哔咔搜索失败: {exc}")
        comics = data.get("comics") or {}
        docs = [doc for doc in (comics.get("docs") or []) if isinstance(doc, dict)]
        if not docs:
            return await self._safe_send(event, f"哔咔没有搜到「{keyword}」")
        lines = [f"哔咔搜索「{keyword}」第 {page} 页（共 {comics.get('total', '?')} 本）："]
        for index, doc in enumerate(docs[:10], 1):
            lines.append(
                f"{index}. {doc.get('title')}  [{doc.get('_id')}]  "
                f"{doc.get('epsCount', '?')}章 {doc.get('pagesCount', '?')}页"
            )
        lines.append("发送 /pica <ID> 开始下载")
        await self._safe_send(event, "\n".join(lines))

    async def _do_pica_detail(self, event: AstrMessageEvent, comic_id: str):
        client = self._get_pica_client()
        if client is None:
            return await self._safe_send(event, "哔咔支持未启用")
        try:
            comic = await self._pica_call(client.comic, comic_id)
        except Exception as exc:
            return await self._safe_send(event, f"获取哔咔详情失败: {exc}")
        if not comic:
            return await self._safe_send(event, f"没有找到哔咔本子 {comic_id}")
        categories = "/".join(
            str(item.get("name")) for item in (comic.get("categories") or []) if isinstance(item, dict)
        )
        lines = [
            f"哔咔《{comic.get('title')}》",
            f"ID: {comic_id}",
            f"作者: {comic.get('author') or '未知'}",
            f"章节: {comic.get('epsCount', '?')}  页数: {comic.get('pagesCount', '?')}",
            f"喜欢: {comic.get('likesCount', '?')}  观看: {comic.get('viewsCount', '?')}",
            f"分类: {categories or '未分类'}",
            f"下载: /pica {comic_id}    仅打包: /pica z {comic_id}",
        ]
        await self._safe_send(event, "\n".join(lines))

    async def _do_pica_episodes(self, event: AstrMessageEvent, comic_id: str, page: int = 1):
        client = self._get_pica_client()
        if client is None:
            return await self._safe_send(event, "哔咔支持未启用")
        try:
            episodes = await self._pica_call(client.episodes, comic_id, timeout=120)
        except Exception as exc:
            return await self._safe_send(event, f"获取章节失败: {exc}")
        if not episodes:
            return await self._safe_send(event, f"哔咔本子 {comic_id} 没有章节")
        ordered = self._select_pica_episodes(episodes, None)
        lines = [f"哔咔本子 {comic_id} 共 {len(ordered)} 章："]
        start = max(0, (page - 1) * 30)
        for index, episode in ordered[start:start + 30]:
            lines.append(f"{index}. {episode.get('title')}")
        if len(ordered) > start + 30:
            lines.append(f"… 发送 /pica eps {comic_id} {page + 1} 看下一页")
        lines.append(
            f"下载全部: /pica {comic_id}    下载某几章: /pica {comic_id} 1-5,8"
        )
        await self._safe_send(event, "\n".join(lines))

    async def _do_pica_ranking(self, event: AstrMessageEvent, rank_type: str):
        client = self._get_pica_client()
        if client is None:
            return await self._safe_send(event, "哔咔支持未启用")
        mapping = {"day": "H24", "week": "D7", "month": "D30"}
        try:
            data = await self._pica_call(
                client.leaderboard, mapping.get(rank_type, "H24"), "VC", timeout=60
            )
        except Exception as exc:
            return await self._safe_send(event, f"获取哔咔排行榜失败: {exc}")
        docs = [
            doc
            for doc in ((data.get("comics") or {}).get("docs") or [])
            if isinstance(doc, dict)
        ]
        if not docs:
            return await self._safe_send(event, "哔咔排行榜暂时没有数据")
        lines = [f"哔咔排行榜（{rank_type}）："]
        for index, doc in enumerate(docs[:10], 1):
            lines.append(f"{index}. {doc.get('title')}  [{doc.get('_id')}]")
        lines.append("发送 /pica <ID> 开始下载")
        await self._safe_send(event, "\n".join(lines))

    async def _do_pica_favorites(self, event: AstrMessageEvent, page: int = 1):
        client = self._get_pica_client()
        if client is None:
            return await self._safe_send(event, "哔咔支持未启用")
        try:
            data = await self._pica_call(client.favorites, page, timeout=60)
        except Exception as exc:
            return await self._safe_send(event, f"获取收藏夹失败: {exc}")
        docs = [
            doc
            for doc in ((data.get("comics") or {}).get("docs") or [])
            if isinstance(doc, dict)
        ]
        if not docs:
            return await self._safe_send(event, f"哔咔收藏夹第 {page} 页是空的")
        lines = [f"哔咔收藏夹第 {page} 页（共 {data.get('total', '?')} 本）："]
        for index, doc in enumerate(docs[:10], 1):
            lines.append(f"{index}. {doc.get('title')}  [{doc.get('_id')}]")
        lines.append("发送 /pica <ID> 开始下载")
        await self._safe_send(event, "\n".join(lines))

    async def _do_pica_rand(self, event: AstrMessageEvent):
        client = self._get_pica_client()
        if client is None:
            return await self._safe_send(event, "哔咔支持未启用")
        for _ in range(3):
            try:
                data = await self._pica_call(client.leaderboard, "H24", "VC", timeout=40)
            except Exception as exc:
                return await self._safe_send(event, f"获取哔咔推荐失败: {exc}")
            docs = [
                doc
                for doc in ((data.get("comics") or {}).get("docs") or [])
                if isinstance(doc, dict) and doc.get("_id")
            ]
            docs = [
                doc for doc in docs if f"pica_{doc['_id']}" not in self._sent_albums
            ]
            if docs:
                pick = random.choice(docs)
                await self._safe_send(
                    event,
                    f"哔咔随机推荐：《{pick.get('title')}》 {pick.get('_id')}，详情如下",
                )
                await self._do_pica_detail(event, str(pick["_id"]))
                self._mark_album_sent(f"pica_{pick['_id']}")
                return
        await self._safe_send(event, "哔咔榜单里的本子都推荐过了，明天再看吧")

    async def _do_pica_login(self, event: AstrMessageEvent):
        if not self._pica_configured():
            return await self._safe_send(
                event, "哔咔未配置账号：请在插件配置里填 pica_account / pica_password"
            )
        client = self._get_pica_client()
        try:
            token = await self._pica_call(client.ensure_token, timeout=45)
            if not token:
                raise RuntimeError("没有拿到 token")
        except Exception as exc:
            return await self._safe_send(event, f"哔咔登录失败: {exc}")
        await self._safe_send(
            event, f"哔咔登录正常（账号 {self._pica_str('pica_account') or 'token 登录'}）"
        )

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    async def _accept_pica_entry(self, event: AstrMessageEvent) -> List[Any]:
        sub, args = self._split_pica_subcommand(event.message_str)
        if sub == "help":
            return [event.plain_result(self._pica_help_text())]
        if not self._pica_configured():
            return [
                event.plain_result(
                    "哔咔未配置账号：请在插件配置里填 pica_account / pica_password"
                )
            ]
        denied = self._check_access(event)
        if denied:
            return [event.plain_result(denied)]
        if sub == "stop":
            return await self._accept_stop(event, args)
        if sub in ("download", "zip"):
            return await self._accept_pica_download(event, args, pack=(sub == "zip"))
        if sub == "search":
            keyword = " ".join(args).strip()
            page = 1
            match = re.search(r"(\d+)\s*$", keyword)
            if match:
                page = max(1, int(match.group(1)))
                keyword = keyword[: match.start()].strip()
            if not keyword:
                return [event.plain_result("用法：/pica s 关键词 [页码]")]
            self._start_task(self._do_pica_search, event, keyword, page)
            return [event.plain_result(f"正在搜索哔咔「{keyword}」…")]
        if sub == "detail":
            if not args:
                return [event.plain_result("用法：/pica d <本子ID>")]
            self._start_task(self._do_pica_detail, event, args[0])
            return [event.plain_result("正在获取哔咔详情…")]
        if sub == "eps":
            if not args:
                return [event.plain_result("用法：/pica eps <本子ID> [页码]")]
            page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
            self._start_task(self._do_pica_episodes, event, args[0], page)
            return [event.plain_result("正在获取章节列表…")]
        if sub == "rank":
            rank_type = "week"
            for token in args:
                lowered = token.lower()
                if lowered in ("day", "today", "日", "今日"):
                    rank_type = "day"
                elif lowered in ("week", "周", "本周"):
                    rank_type = "week"
                elif lowered in ("month", "月", "本月"):
                    rank_type = "month"
            self._start_task(self._do_pica_ranking, event, rank_type)
            return [event.plain_result(f"正在获取哔咔{rank_type}排行榜…")]
        if sub == "rand":
            self._start_task(self._do_pica_rand, event)
            return [event.plain_result("正在挑一本哔咔…")]
        if sub == "fav":
            page = int(args[0]) if args and args[0].isdigit() else 1
            self._start_task(self._do_pica_favorites, event, page)
            return [event.plain_result(f"正在读取哔咔收藏夹第 {page} 页…")]
        if sub == "login":
            self._start_task(self._do_pica_login, event)
            return [event.plain_result("正在检查哔咔登录状态…")]
        return [event.plain_result(self._pica_help_text())]

    async def _accept_pica_download(
        self, event: AstrMessageEvent, args: List[str], pack: bool
    ) -> List[Any]:
        if not pack and not self._pica_pdf_available():
            return [event.plain_result("PDF 库未安装，请先执行：pip install img2pdf Pillow")]
        try:
            comic_id, spec, extra = self._parse_pica_tokens(args)
            ranges = self._parse_chapter_spec(spec)
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
        head = f"哔咔 {comic_id}（{self._chapter_spec_label(spec)}）{action}中"
        if not pack:
            head += f"（质量={quality}）"
        self._start_task(
            self._pica_download_task, event, comic_id, spec, ranges, pack, quality, max_size
        )
        return [
            event.plain_result(f"{head}，正在解析…\n发送「/pica stop」可停止")
        ]

    def _pica_pdf_available(self) -> bool:
        import sys

        module = sys.modules.get(type(self).__module__) or sys.modules.get("__main__")
        return bool(getattr(module, "PDF_AVAILABLE", True))

    @staticmethod
    def _pica_help_text() -> str:
        return (
            "【哔咔 PicACG】\n"
            "\n"
            "/pica 24位本子ID      下载整本，自动转 PDF\n"
            "/pica s 关键词        搜索（也可以直接 /pica 关键词）\n"
            "/pica rand            随机推荐\n"
            "/pica <ID> 1-5        只下第 1~5 章\n"
            "\n"
            "/pica z <ID>          打包成 ZIP\n"
            "/pica d <ID>          只看详情\n"
            "/pica eps <ID>        章节列表\n"
            "/pica r week          排行榜：day / week / month\n"
            "/pica fav             我的收藏\n"
            "/pica stop            停止下载\n"
            "/pica login           检查登录状态\n"
            "/pica help            这条说明\n"
            "\n"
            "中文子命令也认：下载、搜索、排行、详情、章节、收藏、随机、停止、登录、帮助。\n"
            "JM 的命令仍然用 /jm，互不影响。\n"
        )
