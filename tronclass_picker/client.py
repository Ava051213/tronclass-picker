"""HTTP 层：Cookie 会话、限速、重试、登录态检测、流式下载。"""

from __future__ import annotations

import hashlib
import http.cookiejar as cookiejar
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger("http")

# 命中这些特征说明会话已经过期，被重定向回统一认证或登录页
_LOGIN_HINTS = (
    "authserver", "/login", "/cas/", "sso", "oauth2", "passport",
)
_HTML_SNIFF = ("<!doctype html", "<html", "<head", "<script")


class TronClassError(Exception):
    """所有可预期的错误基类。"""


class AuthExpired(TronClassError):
    """会话失效：Cookie 过期、被踢下线，或被 CAS 拦截。"""


class ApiError(TronClassError):
    """接口返回了非 2xx 状态码。"""

    def __init__(self, status: int, path: str, body: str = "") -> None:
        self.status = status
        self.path = path
        self.body = body[:400]
        super().__init__(f"HTTP {status} @ {path} {self.body}".strip())


class NetworkError(TronClassError):
    """连接层面的问题：超时、DNS、断流。"""


class RateLimited(TronClassError):
    def __init__(self, wait: float) -> None:
        self.wait = wait
        super().__init__(f"请求过于频繁，需等待 {wait:.0f}s")


@dataclass
class DownloadResult:
    path: Path
    size: int
    sha1: str
    content_type: str = ""


class HttpClient:
    """带 Cookie 的极简 HTTP 客户端（仅 GET，只读用途）。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.base_url = cfg.base_url
        self.host = urllib.parse.urlsplit(cfg.base_url).hostname or ""
        self._jar = cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            _RedirectRecorder(),
        )
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._stats = {"requests": 0, "retries": 0, "bytes": 0}
        self._install_cookies()

    # ---- 会话 --------------------------------------------------------
    def _install_cookies(self) -> None:
        raw_cookie = self.cfg.auth.cookie.strip()
        if not raw_cookie and self.cfg.auth.cookie_file:
            raw_cookie = _read_cookie_file(Path(self.cfg.auth.cookie_file).expanduser())
        if not raw_cookie:
            return

        count = 0
        for item in raw_cookie.replace("\n", ";").split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            name, _, value = item.partition("=")
            name, value = name.strip(), value.strip().strip('"')
            if not name:
                continue
            cookie = cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=self.host, domain_specified=True, domain_initial_dot=False,
                path="/", path_specified=True, secure=False, expires=None,
                discard=False, comment=None, comment_url=None, rest={},
            )
            with self._lock:
                self._jar.set_cookie(cookie)
            count += 1
        log.debug("已注入 %d 个 Cookie（域 %s）", count, self.host)

    @property
    def cookie_count(self) -> int:
        with self._lock:
            return len(list(self._jar))

    def describe_session(self) -> str:
        names = sorted({c.name for c in self._jar})
        return f"{len(names)} 个 Cookie：{', '.join(names[:6])}" if names else "无 Cookie"

    # ---- 节流 --------------------------------------------------------
    def _throttle(self) -> None:
        interval = self.cfg.network.request_interval
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last_request + interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_request = now

    # ---- 基础请求 ----------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.cfg.auth.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/",
        }
        headers.update(self.cfg.auth.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    def _open(self, url: str, extra_headers: dict[str, str] | None = None):
        request = urllib.request.Request(url, headers=self._headers(extra_headers))
        try:
            return self._opener.open(request, timeout=self.cfg.network.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthExpired(
                    f"会话已失效（HTTP {exc.code}），请重新获取 Cookie"
                ) from exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                raise RateLimited(float(retry_after or 60)) from exc
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise ApiError(exc.code, url, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"无法连接 {url}：{reason}") from exc

    def _final_url(self, response) -> str:
        getter = getattr(response, "geturl", None)
        return getter() if callable(getter) else response.full_url

    @staticmethod
    def _looks_like_login(final_url: str, body: bytes, content_type: str) -> bool:
        lowered = final_url.lower()
        if any(hint in lowered for hint in _LOGIN_HINTS) and "lms." not in lowered:
            return True
        if "text/html" not in content_type:
            return False
        head = body[:2048].decode("utf-8", "replace").strip().lower()
        if not any(tag in head for tag in _HTML_SNIFF):
            return False
        # 返回了 HTML 而不是 JSON，且页面里出现登录语汇 → 判定为掉登录
        return any(word in head for word in ("登录", "login", "password", "统一身份"))

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET 一个 JSON 接口，带重试。"""
        url = path if path.startswith("http") else urllib.parse.urljoin(
            self.base_url + "/", path.lstrip("/")
        )
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "", [])}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)

        attempts = max(1, self.cfg.network.retries)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                with self._open(url) as response:
                    body = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    final_url = self._final_url(response)
                    self._stats["requests"] += 1

                    if self._looks_like_login(final_url, body, content_type):
                        raise AuthExpired("返回的是登录页，会话已失效")

                    text = body.decode("utf-8", "replace").lstrip("\ufeff")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        # 少数接口返回 JS 包裹的 JSON，或空体
                        if not text.strip():
                            return {}
                        snippet = text[:200]
                        raise ApiError(200, url, f"响应不是 JSON：{snippet}") from None
            except (AuthExpired, ApiError) as exc:
                if isinstance(exc, ApiError) and exc.status < 500:
                    raise
                last_error = exc
            except RateLimited as exc:
                last_error = exc
                log.warning("触发限流，等待 %.0fs 后重试（%d/%d）", exc.wait, attempt, attempts)
                time.sleep(exc.wait)
                continue
            except NetworkError as exc:
                last_error = exc

            if attempt < attempts:
                backoff = self.cfg.network.retry_backoff ** attempt
                backoff += random.uniform(0, 0.5)
                log.warning(
                    "请求失败（%d/%d），%.1fs 后重试：%s", attempt, attempts, backoff, last_error
                )
                self._stats["retries"] += 1
                time.sleep(backoff)

        raise last_error or TronClassError(f"请求失败：{url}")

    # ---- 下载 --------------------------------------------------------
    def download(
        self,
        url: str,
        dest: Path,
        max_bytes: int = 0,
        referer: str | None = None,
    ) -> DownloadResult:
        """流式下载到 dest。先写 .part，校验大小后再改名，避免半截文件。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        digest = hashlib.sha1()
        size = 0
        headers = {"Referer": referer} if referer else None

        content_type = ""
        self._throttle()
        try:
            with self._open(url, headers) as response:
                content_type = response.headers.get("Content-Type", "")
                declared = response.headers.get("Content-Length")
                declared_size = int(declared) if declared and declared.isdigit() else 0
                if max_bytes and declared_size and declared_size > max_bytes:
                    raise TronClassError(
                        f"文件 {declared_size / 1048576:.1f}MB 超过上限"
                        f" {max_bytes / 1048576:.0f}MB，已跳过"
                    )
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if max_bytes and size > max_bytes:
                            raise TronClassError("文件超过大小上限，已中断")
                        digest.update(chunk)
                        fh.write(chunk)
                self._stats["requests"] += 1
                self._stats["bytes"] += size

            if declared_size and size != declared_size:
                raise NetworkError(
                    f"下载不完整：{size}/{declared_size} 字节"
                )
            if size == 0:
                raise NetworkError("下载到 0 字节")

            if dest.exists():
                dest.unlink()
            tmp.replace(dest)
            return DownloadResult(dest, size, digest.hexdigest(), content_type)
        except (AuthExpired, ApiError, NetworkError, TronClassError):
            tmp.unlink(missing_ok=True)
            raise
        except Exception as exc:  # 磁盘写满、权限不足等
            tmp.unlink(missing_ok=True)
            raise NetworkError(f"写入 {dest} 失败：{exc}") from exc

    # ---- 统计 --------------------------------------------------------
    def stats(self) -> dict[str, int]:
        return dict(self._stats)


class _RedirectRecorder(urllib.request.HTTPRedirectHandler):
    """记录重定向链，便于判断是否被踢回登录页。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        log.debug("重定向 %s → %s", req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_cookie_file(path: Path) -> str:
    """读取 Netscape 格式的 cookies.txt，拼成 Cookie 头。"""
    if not path.is_file():
        log.warning("cookie_file 不存在：%s", path)
        return ""
    pairs: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            pairs.append(f"{parts[5]}={parts[6]}")
    return "; ".join(pairs)


def parse_cookie_input(text: str) -> str:
    """把用户粘贴的内容规整成 Cookie 头。

    接受三种常见形态：纯 Cookie 串、带 "Cookie:" 前缀的请求头、多行 key=value。
    """
    text = text.strip()
    if not text:
        return ""
    if "cookie:" in text.lower()[:20]:
        text = text.split(":", 1)[1]
    text = text.replace("\r", "").replace("\n", "; ")
    parts = [p.strip() for p in text.split(";") if p.strip() and "=" in p]
    return "; ".join(parts)
