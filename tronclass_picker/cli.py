"""命令行入口：解析参数 → 构造 Selection → 交给 App 执行。

这一层不做业务，只做三件事：
    1. 把 argv 翻译成 Selection（或交给向导产出 Selection）
    2. 调 App 的三步：connect → plan → run
    3. 把 JobResult 翻译成退出码
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, interactive, login, reporting
from .app import App
from .client import AuthExpired, NetworkError, TronClassError
from .config import Config, ConfigError, dump_default, load
from .exits import EXIT_AUTH, EXIT_CONFIG, EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from .logx import setup as setup_logging
from .models import Course, JobResult, Selection
from .planning import select_courses

log = logging.getLogger("main")

BANNER = """
┌──────────────────────────────────────────────────────────────┐
│  畅课课程归档工具  tronclass-picker  v{version}                    │
└──────────────────────────────────────────────────────────────┘
  使用须知
  · 仅读取你账号本就有权限查看的课程内容，不绕过任何访问控制
  · 不下载平台标注为禁止下载、或需要额外授权的内容
  · 请遵守畅课平台服务条款及所在学校的网络与信息管理规定
  · 请求已内置间隔限制，请勿调低至可能影响平台运行的程度
  · 归档结果仅供个人复习使用，请勿二次传播
"""


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tronclass-picker",
        description="下载畅课（TronClass）课程文档，并汇总课程通知。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m tronclass_picker init            生成 config.json\n"
            "  python -m tronclass_picker login           浏览器登录并保存 Cookie\n"
            "  python -m tronclass_picker check           验证会话与课程列表\n"
            "  python -m tronclass_picker web             打开网页界面（推荐）\n"
            "  python -m tronclass_picker pick            命令行里逐项选择\n"
            "  python -m tronclass_picker                 按配置下载文档 + 汇总通知\n"
            "  python -m tronclass_picker --only notices  只汇总通知\n"
            "  python -m tronclass_picker --interval 6h   每 6 小时跑一次\n"
        ),
    )
    parser.add_argument(
        "command", nargs="?",
        choices=["init", "login", "check", "pick", "web", "run"],
        help="留空等同 run",
    )
    parser.add_argument("-c", "--config", default=None, help="配置文件路径（默认 ./config.json）")
    parser.add_argument("-o", "--output", help="覆盖输出目录")
    parser.add_argument(
        "--only", choices=["docs", "notices", "all"], default="all", help="只处理某一类"
    )
    parser.add_argument(
        "--course", action="append", default=[], metavar="关键字",
        help="只下载匹配该关键字的课程，可重复；不影响通知采集",
    )
    parser.add_argument("--types", metavar="pdf,pptx", help="临时覆盖下载的文件类型，逗号分隔")
    parser.add_argument(
        "--include-restricted", action="store_true",
        help="尝试下载平台标记为禁止下载的文件（能否取到取决于平台接口是否放行）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出将要下载的文件")
    parser.add_argument("--interval", help='定时运行间隔，如 "30m"、"6h"')
    parser.add_argument(
        "--paste", action="store_true", help="配合 login 使用：手动粘贴 Cookie，不装浏览器驱动"
    )
    parser.add_argument("--no-banner", action="store_true", help="不打印使用须知")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出警告与错误")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "run"

    if command == "init":
        return _cmd_init(args)

    if command == "web":
        from . import web

        return web.main(args.config)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.output:
        cfg.paths.output = args.output

    setup_logging(cfg.logs_dir, _level(args), quiet=args.quiet)

    if command == "login":
        return login.run(cfg, _config_path(args), paste=args.paste)

    if not args.no_banner and not args.quiet and command != "pick":
        print(BANNER.format(version=__version__))

    # 向导必须跑在真终端里
    if command == "pick" and not interactive.is_interactive():
        log.error("当前不是交互终端，无法使用选择模式。")
        log.error("请改用命令行参数，例如：")
        log.error("  python -m tronclass_picker --only docs --course 关键字")
        return EXIT_CONFIG

    # 双击启动时没凭据就先登录，登录完接着走
    if command == "pick" and not cfg.auth.cookie and not cfg.auth.cookie_file:
        print("还没有登录凭据，先做一次登录。")
        print()
        code = login.run(cfg, _config_path(args), paste=args.paste)
        if code != EXIT_OK:
            return code
        try:
            cfg = load(args.config)
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return EXIT_CONFIG
        if args.output:
            cfg.paths.output = args.output
        setup_logging(cfg.logs_dir, _level(args), quiet=args.quiet)
        print()

    if not cfg.auth.cookie and not cfg.auth.cookie_file:
        log.error("尚未配置登录凭据。请先执行：python -m tronclass_picker login")
        log.error("或把浏览器里的 Cookie 填入 config.json 的 auth.cookie")
        return EXIT_AUTH

    interval = parse_interval(args.interval or cfg.run.interval)
    if interval > 0 and command == "run":
        return _loop(cfg, args, interval)
    return _cycle(cfg, args, command)


# --------------------------------------------------------------------------
# 一轮完整流程
# --------------------------------------------------------------------------
def _cycle(cfg: Config, args: argparse.Namespace, command: str) -> int:
    log.info("开始运行：%s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    app = App(cfg)
    log.info("会话：%s", app.sessions)

    try:
        who, courses = app.connect()
    except AuthExpired as exc:
        if command != "pick":
            log.error("登录已失效：%s", exc)
            log.error("请重新执行：python -m tronclass_picker login")
            return EXIT_AUTH

        # 双击启动的场景：登录过期就地重登一次，不让用户再去敲命令
        print()
        print("登录状态过期了，重新登录一次。")
        print()
        if login.run(cfg, _config_path(args)) != EXIT_OK:
            return EXIT_AUTH
        try:
            cfg = load(args.config)
        except ConfigError as exc:
            print(f"配置错误：{exc}", file=sys.stderr)
            return EXIT_CONFIG
        if args.output:
            cfg.paths.output = args.output
        setup_logging(cfg.logs_dir, _level(args), quiet=args.quiet)
        app = App(cfg)
        try:
            who, courses = app.connect()
        except AuthExpired:
            log.error("重新登录后仍无法访问课程，请检查账号权限。")
            return EXIT_AUTH
    except (NetworkError, TronClassError) as exc:
        log.error("获取课程列表失败：%s", exc)
        return EXIT_FATAL

    if command == "check":
        return _report_courses(cfg, courses, args, who)

    try:
        if command == "pick":
            selection = interactive.ask(app, cfg, courses, who)
            print()
        else:
            selection = build_selection(cfg, courses, args)

        log.info("范围：%s｜文档 %s｜通知 %s",
                 selection.label,
                 "开" if selection.want_documents else "关",
                 "开" if selection.want_notices else "关")

        job = app.plan(selection)
        if job.blocked:
            log.info(
                "另有 %d 个受限文件未纳入，加 --include-restricted 可一并尝试", job.blocked
            )
        if job.empty:
            log.warning("这次没有任何可处理的内容。")
            return EXIT_OK

        result = app.run(job, dry_run=args.dry_run)
    except interactive.Cancelled as exc:
        if str(exc):
            print(f"\n{exc}")
        print("已取消。")
        return EXIT_OK
    except KeyboardInterrupt:
        print("\n已取消。")
        return EXIT_OK
    except AuthExpired as exc:
        log.error("执行过程中登录失效：%s", exc)
        log.error("请重新执行：python -m tronclass_picker login")
        return EXIT_AUTH

    reporting.log_summary(result)
    return _exit_code(result)


def _loop(cfg: Config, args: argparse.Namespace, interval: int) -> int:
    max_runs = cfg.run.max_runs
    index = 0
    code = EXIT_OK
    log.info(
        "定时模式：每 %d 分钟一轮%s",
        interval // 60,
        f"，最多 {max_runs} 轮" if max_runs else "，Ctrl+C 退出",
    )
    try:
        while True:
            index += 1
            log.info("=" * 20 + f" 第 {index} 轮 " + "=" * 20)
            code = _cycle(cfg, args, "run")
            if code == EXIT_AUTH:
                log.error("会话失效，定时任务中止。重新登录后再启动。")
                return code
            if max_runs and index >= max_runs:
                break
            log.info("下一轮将在 %d 分钟后开始", interval // 60)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("已手动停止")
    return code


def build_selection(cfg: Config, courses: list[Course], args: argparse.Namespace) -> Selection:
    """把命令行参数翻译成 Selection。"""
    picked = select_courses(courses, cfg, extra_include=args.course)
    extensions = None
    if args.types:
        extensions = [
            t.strip() if t.strip().startswith(".") else f".{t.strip()}"
            for t in args.types.split(",")
            if t.strip()
        ]
    return Selection(
        courses=picked,
        want_documents=args.only in ("docs", "all") and cfg.documents.enabled,
        want_notices=args.only in ("notices", "all") and cfg.notices.enabled,
        extensions=extensions,
        label=f"配置筛选后 {len(picked)} 门课程",
        notices_follow=not cfg.notices.all_courses,
        include_restricted=args.include_restricted,
    )


def _report_courses(
    cfg: Config, courses: list[Course], args: argparse.Namespace, who: str
) -> int:
    selected = select_courses(courses, cfg, extra_include=args.course)
    log.info("登录有效，当前用户：%s", who or "（接口未返回姓名）")
    log.info("共 %d 门课程，按当前筛选规则命中 %d 门：", len(courses), len(selected))
    for course in selected:
        log.info("  · %s  [id=%s]  %s", course.display, course.id, course.semester)
    return EXIT_OK


def _exit_code(result: JobResult) -> int:
    return EXIT_PARTIAL if result.failed else EXIT_OK


# --------------------------------------------------------------------------
# 子命令：init / login
# --------------------------------------------------------------------------
def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config or "config.json")
    if target.exists():
        print(f"已存在 {target}，未覆盖。如需重新生成请先删除该文件。")
        return EXIT_OK
    path = dump_default(target)
    print(f"已生成配置模板：{path}")
    print("下一步：")
    print("  1) 打开该文件，确认 base_url 是你学校的畅课地址")
    print("  2) 执行  python -m tronclass_picker login  完成登录")
    print("  3) 执行  python -m tronclass_picker check  验证能否读到课程")
    return EXIT_OK


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _config_path(args: argparse.Namespace) -> Path:
    return Path(args.config or "config.json")


def parse_interval(text: str | None) -> int:
    """把 "30m" / "6h" / "90s" / "1d" 解析成秒；无法解析返回 0。"""
    if not text:
        return 0
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(text).lower())
    if not match:
        log.warning("无法解析运行间隔「%s」，按单次执行处理", text)
        return 0
    value = int(match.group(1))
    unit = match.group(2) or "m"
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _level(args: argparse.Namespace) -> int:
    return logging.DEBUG if args.verbose else logging.INFO


def run() -> None:
    """供 console_scripts 或 __main__ 调用。"""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
