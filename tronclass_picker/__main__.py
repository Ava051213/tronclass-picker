"""python -m tronclass_picker：起一个本地服务，网页和外部接口共用同一个进程。

另有 login / check 两个轻量子命令：login 走浏览器或手动粘贴拿 Cookie，
check 用配置里的 Cookie 探一次个人接口，验证会话是否还有效。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import login
from .api import Api
from .client import HttpClient, TronClassError
from .config import ConfigError, load
from .server import serve


def _cmd_login(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tronclass_picker login",
        description="打开浏览器完成统一认证抓取 Cookie；加 --paste 则手动粘贴",
    )
    parser.add_argument("--paste", action="store_true", help="手动粘贴浏览器请求头里的 Cookie")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config.json")
    args = parser.parse_args(rest)
    config_path = Path(args.config or "config.json")
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(str(exc))
        return login.NO_CONFIG
    return login.run(cfg, config_path, paste=args.paste)


def _cmd_check(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tronclass_picker check",
        description="验证 config.json 里的会话是否有效（向平台发一次只读请求）",
    )
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config.json")
    args = parser.parse_args(rest)
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(str(exc))
        return login.NO_CONFIG

    http = HttpClient(cfg)
    try:
        profile = Api(http).profile()
    except TronClassError as exc:
        print(f"会话无效：{exc}")
        print("先执行  python -m tronclass_picker login  重新登录。")
        return login.NO_AUTH
    if not profile:
        print("会话无效：Cookie 缺失或已被清空。")
        print("先执行  python -m tronclass_picker login  重新登录。")
        return login.NO_AUTH

    name = (
        profile.get("name")
        or profile.get("display_name")
        or profile.get("username")
        or profile.get("login")
        or ""
    )
    print(f"会话有效：{name}" if name else "会话有效")
    print(f"  {http.describe_session()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # login / check 不起服务，直接走子命令
    if args and args[0] == "login":
        return _cmd_login(args[1:])
    if args and args[0] == "check":
        return _cmd_check(args[1:])

    parser = argparse.ArgumentParser(
        prog="tronclass_picker",
        description="畅课归档 + 待办接口",
    )
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config.json")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取 server.port")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="启动时不自动扫描，只提供接口",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="把运行日志写进指定文件，开机自启时用",
    )
    parsed = parser.parse_args(args)
    return serve(
        parsed.config,
        open_browser=not parsed.no_browser,
        port=parsed.port,
        auto_scan=not parsed.no_scan,
        log_path=parsed.log,
    )


if __name__ == "__main__":
    sys.exit(main())
