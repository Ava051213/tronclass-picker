"""登录：用真实浏览器抓取 Cookie，或手动粘贴。

畅课的登录由学校统一身份认证完成（多数带多因子），脚本不模拟账密，
只复用浏览器里已经登录成功的会话。优先驱动系统已装的 Chrome / Edge，
避免额外下载 Playwright 自带的 Chromium。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

from .client import parse_cookie_input
from .config import DEFAULT_BASE_URL, Config
from .exits import EXIT_AUTH, EXIT_CONFIG, EXIT_OK

log = logging.getLogger("login")


class _NoBrowserError(Exception):
    """所有浏览器通道都启动失败。"""


def run(cfg: Config, config_path: Path, paste: bool = False, wait=None) -> int:
    """wait 为 None 时用 input() 等回车；传 wait_for_login 则自动轮询，GUI 用这个。"""
    if paste:
        return _paste(cfg, config_path)
    return _browser(cfg, config_path, wait)


# --------------------------------------------------------------------------
# 浏览器
# --------------------------------------------------------------------------
def _browser(cfg: Config, config_path: Path, wait=None) -> int:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print("自动登录需要 Playwright（只装库，不用下浏览器）：")
        print("    pip install playwright")
        print()
        print("不想装也行，手动方式同样有效：")
        print(f"  1) 浏览器打开 {cfg.base_url} 并完成登录")
        print("  2) F12 → Network → 任选一个请求 → 复制请求头里的 Cookie 整行")
        print("  3) 执行  python -m tronclass_picker login --paste")
        print()
        return EXIT_CONFIG

    print(f"正在启动浏览器：{cfg.base_url}")
    with sync_playwright() as pw:
        try:
            context = _open_browser(pw, cfg)
        except _NoBrowserError as exc:
            print()
            print("没能启动浏览器。依次尝试了：")
            print(str(exc))
            print()
            print("任选一条路走：")
            print("  · 装一个 Chrome 或 Edge（本机已有就直接可用，不用下载浏览器内核）")
            print("  · 或者执行  playwright install chromium  下载 130MB 内核")
            print("  · 或者改用  python -m tronclass_picker login --paste")
            return EXIT_CONFIG

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(60000)
            page.goto(cfg.base_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)

            if _logged_in(page.url):
                print("检测到已经是登录状态，直接导出 Cookie。")
            elif wait is not None:
                if not wait(page):
                    print("等待登录超时，已放弃。")
                    return EXIT_AUTH
                print("登录成功，正在导出 Cookie。")
            else:
                print("请在浏览器窗口完成统一身份认证登录。")
                input(">>> 登录完成后，回到本窗口按回车继续…")

            cookies = context.cookies()
        finally:
            context.close()

    host = re.sub(r"^https?://", "", cfg.base_url).split("/")[0]
    jar = [c for c in cookies if host.endswith(c.get("domain", "").lstrip("."))]
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in jar)
    if not cookie_str:
        print("没有捕获到任何 Cookie，可能是登录没有完成。")
        return EXIT_AUTH

    persist_cookie(config_path, cookie_str)
    print(f"已保存 {len(jar)} 个 Cookie 到 {config_path}")
    print("接着执行  python -m tronclass_picker check  验证会话。")
    return EXIT_OK


def _open_browser(pw, cfg: Config):
    """按 chrome → edge → Playwright 自带内核的顺序挑一个能用的。"""
    profile_dir = cfg.output_dir / ".browser"
    profile_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    for channel in ("chrome", "msedge", None):
        label = channel or "Playwright 自带内核"
        kwargs: dict = {
            "headless": False,
            "no_viewport": True,
            "args": ["--start-maximized"],
        }
        if channel:
            kwargs["channel"] = channel
        try:
            return pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        except Exception as exc:  # 缺内核、端口占用、权限等
            first_line = str(exc).splitlines()[0][:110]
            log.debug("浏览器通道 %s 启动失败：%s", label, exc)
            errors.append(f"  · {label}：{first_line}")
    raise _NoBrowserError("\n".join(errors))


def _logged_in(url: str) -> bool:
    """跳到了统一认证或登录页就说明还没登录。"""
    lowered = url.lower()
    return not any(k in lowered for k in ("authserver", "/login", "/cas/", "passport"))


def wait_for_login(page, timeout: int = 300) -> bool:
    """等用户在浏览器里完成登录，最多等 timeout 秒。GUI 用这个代替 input()。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _logged_in(page.url):
                return True
        except Exception:  # 页面正在跳转时取 url 可能失败
            pass
        time.sleep(1)
    return False


# --------------------------------------------------------------------------
# 手动粘贴
# --------------------------------------------------------------------------
def _paste(cfg: Config, config_path: Path) -> int:
    print("把浏览器请求头中的 Cookie 整行粘贴到下面，回车结束：")
    raw = sys.stdin.readline()
    cookie_str = parse_cookie_input(raw)
    if not cookie_str:
        print("输入为空，已取消。")
        return EXIT_AUTH

    persist_cookie(config_path, cookie_str)
    print(f"已写入 {config_path}")
    return EXIT_OK


def persist_cookie(config_path: Path, cookie_str: str) -> None:
    """只改 auth.cookie，其余配置原样保留。"""
    data: dict = {}
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("base_url", DEFAULT_BASE_URL)
    data.setdefault("auth", {})
    data["auth"]["cookie"] = cookie_str
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
