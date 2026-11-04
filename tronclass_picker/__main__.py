"""python -m tronclass_picker：起一个本地服务，网页和外部接口共用同一个进程。"""

from __future__ import annotations

import argparse
import sys

from .server import serve


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
    return serve(
        args.config,
        open_browser=not args.no_browser,
        port=args.port,
        auto_scan=not args.no_scan,
        log_path=args.log,
    )


if __name__ == "__main__":
    sys.exit(main())
