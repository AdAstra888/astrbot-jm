# -*- coding: utf-8 -*-
"""PicACG（哔咔漫画）异步 API 客户端。

只有协议层：签名、登录、token 缓存、搜索/详情/章节/图片/收藏/排行榜、图片下载。
不依赖 astrbot，方便单测。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

API_BASE = "https://picaapi.picacomic.com/"
API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
API_SECRET = "~d}$Q7$eIni=V)9\\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
API_NONCE = "b1ab87b4800d4d4590a11701b8551afa"
APP_VERSION = "2.2.1.2.3.3"
APP_CHANNEL = "2"
APP_BUILD = "45"
APP_UUID = "defaultUuid"
IMAGE_UA = "okhttp/3.8.1"


class PicaError(Exception):
    """哔咔接口错误。"""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class PicaAuthError(PicaError):
    """登录失败或 token 失效。"""


def _sign(path: str, timestamp: str, method: str) -> str:
    raw = f"{path}{timestamp}{API_NONCE}{method.upper()}{API_KEY}"
    return hmac.new(API_SECRET.encode(), raw.lower().encode(), hashlib.sha256).hexdigest()


def _safe_name(text: str, limit: int = 60) -> str:
    cleaned = "".join(ch for ch in str(text or "") if ch not in '\\/:*?"<>|\r\n\t')
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned[:limit] or "untitled").rstrip(" .")


class PicaClient:
    def __init__(
        self,
        account: str = "",
        password: str = "",
        token: str = "",
        proxy: str = "",
        verify_ssl: bool = True,
        token_file: Optional[Path] = None,
        timeout: int = 40,
        api_base: str = "",
        allow_http_fallback: bool = False,
    ):
        self.account = (account or "").strip()
        self.password = password or ""
        self.proxy = (proxy or "").strip()
        self.verify_ssl = bool(verify_ssl)
        self.token_file = Path(token_file) if token_file else None
        self.timeout = max(10, int(timeout))
        self.api_base = (api_base or API_BASE).strip().rstrip("/") + "/"
        # 默认不允许把 https 图片降级成 http：只在明确开启时才尝试
        self.allow_http_fallback = bool(allow_http_fallback)
        self.token = (token or "").strip()
        self._session: Optional[aiohttp.ClientSession] = None
        self._login_lock = asyncio.Lock()
        if not self.token:
            self._load_token()

    # ------------------------------------------------------------------
    # 会话与签名
    # ------------------------------------------------------------------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=aiohttp.TCPConnector(ssl=False if not self.verify_ssl else None),
            )
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _headers(self, path: str, method: str) -> Dict[str, str]:
        ts = str(int(time.time()))
        headers = {
            "api-key": API_KEY,
            "accept": "application/vnd.picacomic.com.v1+json",
            "app-channel": APP_CHANNEL,
            "nonce": API_NONCE,
            "app-version": APP_VERSION,
            "app-uuid": APP_UUID,
            "app-platform": "android",
            "app-build-version": APP_BUILD,
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": IMAGE_UA,
            "image-quality": "original",
            "time": ts,
            "signature": _sign(path, ts, method),
        }
        if self.token:
            headers["authorization"] = self.token
        return headers

    async def _request(self, method: str, path: str, *, json_body: Any = None,
                       need_auth: bool = True, retry_auth: bool = True) -> Dict[str, Any]:
        if need_auth and not self.token:
            await self.ensure_token()
        if need_auth and self.token:
            await self.ensure_token()
        headers = self._headers(path, method)
        session = await self._get_session()
        url = self.api_base + path
        kwargs: Dict[str, Any] = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if self.proxy:
            kwargs["proxy"] = self.proxy
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                async with session.request(method.upper(), url, **kwargs) as resp:
                    text = await resp.text()
                    status = resp.status
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                raise PicaError(f"网络请求失败: {exc}") from exc
        else:  # pragma: no cover
            raise PicaError(f"网络请求失败: {last_error}")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PicaError(f"接口返回非 JSON（HTTP {status}）: {text[:120]}") from exc

        code = data.get("code")
        if code == 200:
            return data.get("data") or {}
        message = str(data.get("message") or data.get("error") or f"HTTP {status}")
        if need_auth and code in (401, 403) and retry_auth:
            await self.ensure_token(force=True)
            return await self._request(method, path, json_body=json_body,
                                       need_auth=need_auth, retry_auth=False)
        if code in (401, 403):
            raise PicaAuthError(f"登录状态失效: {message}", code=code)
        raise PicaError(message, code=code if isinstance(code, int) else None)

    # ------------------------------------------------------------------
    # 登录与 token
    # ------------------------------------------------------------------
    def _load_token(self):
        if not self.token_file or not self.token_file.exists():
            return
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        stored_account = str(data.get("account") or "")
        if self.account and stored_account and stored_account != self.account:
            return
        self.token = str(data.get("token") or "").strip()

    def _save_token(self):
        if not self.token_file or not self.token:
            return
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"account": self.account, "token": self.token, "updated": int(time.time())},
                ensure_ascii=False,
            )
            tmp = self.token_file.with_name(self.token_file.name + ".part")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.token_file)
            try:  # POSIX 下收紧权限；Windows 会忽略或报错，都无害
                os.chmod(self.token_file, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    async def ensure_token(self, force: bool = False):
        if self.token and not force:
            return
        async with self._login_lock:
            if self.token and not force:
                return
            await self.login()

    async def login(self) -> str:
        if not self.account or not self.password:
            if self.token:
                return self.token
            raise PicaAuthError("未配置哔咔账号或密码")
        data = await self._request(
            "POST",
            "auth/sign-in",
            json_body={"email": self.account, "password": self.password},
            need_auth=False,
        )
        token = str(data.get("token") or "").strip()
        if not token:
            raise PicaAuthError("登录接口没有返回 token")
        self.token = token
        self._save_token()
        return token

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def search(self, keyword: str, page: int = 1, sort: str = "dd") -> Dict[str, Any]:
        return await self._request(
            "POST", f"comics/advanced-search?page={int(page)}",
            json_body={"keyword": keyword, "sort": sort},
        )

    async def comic(self, comic_id: str) -> Dict[str, Any]:
        data = await self._request("GET", f"comics/{comic_id}")
        return data.get("comic") or {}

    async def episodes(self, comic_id: str, max_pages: int = 40) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        seen = set()
        page = 1
        while page <= max_pages:
            data = await self._request("GET", f"comics/{comic_id}/eps?page={page}")
            eps = data.get("eps") or {}
            docs = [doc for doc in (eps.get("docs") or []) if isinstance(doc, dict)]
            added = 0
            for doc in docs:
                key = str(doc.get("_id") or "")
                if key and key not in seen:
                    seen.add(key)
                    result.append(doc)
                    added += 1
            total_pages = int(eps.get("pages") or 1)
            if added == 0 or page >= total_pages:
                break
            page += 1
        return result

    async def pages(self, comic_id: str, episode_id: str, max_pages: int = 20) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        seen = set()
        page = 1
        while page <= max_pages:
            data = await self._request(
                "GET", f"comics/{comic_id}/order/{episode_id}/pages?page={page}"
            )
            docs = [doc for doc in ((data.get("pages") or {}).get("docs") or []) if isinstance(doc, dict)]
            added = 0
            for doc in docs:
                key = str(doc.get("_id") or doc.get("media", {}).get("path") or "")
                if key and key not in seen:
                    seen.add(key)
                    result.append(doc)
                    added += 1
            total_pages = int((data.get("pages") or {}).get("pages") or 1)
            if added == 0 or page >= total_pages:
                break
            page += 1
        return result

    async def favorites(self, page: int = 1, sort: str = "dd") -> Dict[str, Any]:
        return await self._request("GET", f"users/favourite?page={int(page)}&s={sort}")

    async def leaderboard(self, tt: str = "H24", ct: str = "VC") -> Dict[str, Any]:
        return await self._request("GET", f"comics/leaderboard?tt={tt}&ct={ct}")

    async def categories(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "categories")
        return data.get("categories") or []

    # ------------------------------------------------------------------
    # 图片
    # ------------------------------------------------------------------
    @staticmethod
    def image_url(page: Dict[str, Any]) -> str:
        media = page.get("media") or {}
        server = str(media.get("fileServer") or "").rstrip("/")
        path = str(media.get("path") or "")
        if not path:
            return ""
        if not path.startswith("/"):
            path = "/" + path
        return f"{server}/static{path}"

    async def download_image(self, url: str, dest: Path) -> Path:
        if not url:
            raise PicaError("图片地址为空")
        dest.parent.mkdir(parents=True, exist_ok=True)
        session = await self._get_session()
        headers = {"User-Agent": IMAGE_UA, "Referer": "https://picacomic.com/"}
        last_error: Optional[Exception] = None
        candidates = [url]
        if self.allow_http_fallback and url.startswith("https://"):
            candidates.append("http://" + url[len("https://"):])
        for candidate in candidates:
            for attempt in range(2):
                try:
                    async with session.get(candidate, headers=headers, proxy=self.proxy or None) as resp:
                        if resp.status != 200:
                            last_error = PicaError(f"HTTP {resp.status}")
                            break
                        payload = await resp.read()
                    if not payload:
                        last_error = PicaError("图片内容为空")
                        break
                    tmp = dest.with_name(dest.name + ".part")
                    tmp.write_bytes(payload)
                    os.replace(tmp, dest)
                    return dest
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    last_error = exc
                    await asyncio.sleep(0.5)
        raise PicaError(f"图片下载失败: {last_error}")
