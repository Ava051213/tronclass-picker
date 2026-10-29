"""交互式选择向导：启动后一步步挑课程、类型、文件、任务。

向导只负责问询，产出 Selection 交回给调用方执行。
只在真正的终端里启用；被重定向或接到管道时由调用方退回命令行参数模式。
"""

from __future__ import annotations

import sys
from datetime import datetime

from .config import Config
from .models import Course, DocTask, Selection

class Cancelled(Exception):
    """用户主动放弃。"""


def is_interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def ask(app, cfg: Config, courses: list[Course], who: str = "") -> Selection:
    """跑完整套提问，返回用户的选择。抛 Cancelled 表示退出。"""
    _header(who)

    action = _main_menu(courses)
    want_docs = action in ("download", "both")
    want_notices = action in ("notices", "both")

    picked = list(courses)
    scope_label = "全部课程"
    tasks: list[DocTask] | None = None
    include_restricted = False
    notices_follow = True

    if want_docs:
        picked, scope_label = _ask_scope(courses)
        print()
        print(f"正在扫描 {len(picked)} 门课程的文件，请稍候…")
        result = app.scan(picked)

        candidates = result.tasks
        if result.restricted_count:
            print()
            print(f"其中 {result.restricted_count} 个文件被老师关闭了下载。")
            print("这类文件网页上没有下载按钮，脚本会照常走标准接口取直链，")
            print("能否成功看平台是否放行；失败会记进日志，不做进一步破解。")
            include_restricted = _yes("要不要也列出来一起挑？", default=False)
            if not include_restricted:
                candidates = [t for t in candidates if not t.restricted]

        tasks = _ask_files(candidates)
        if not tasks:
            print("没有选中任何文件，跳过下载。")
            want_docs = False
            tasks = None
    else:
        # 只汇总通知时不用选课程，直接扫全部
        notices_follow = not cfg.notices.all_courses

    if not want_docs and not want_notices:
        print()
        print("两项都没选，已取消。")
        raise Cancelled()

    _ask_confirm(
        picked, scope_label, want_docs, want_notices,
        len(tasks or []), len(courses) if not want_docs else 0,
    )
    return Selection(
        courses=picked,
        want_documents=want_docs,
        want_notices=want_notices,
        tasks=tasks,
        label=scope_label,
        notices_follow=notices_follow,
        include_restricted=include_restricted,
    )


# --------------------------------------------------------------------------
# 主菜单
# --------------------------------------------------------------------------
_MENU: list[tuple[str, str]] = [
    ("下载课程文件", "download"),
    ("汇总最新通知", "notices"),
    ("两样都做", "both"),
    ("查看我的课程", "list"),
    ("退出", "quit"),
]


def _main_menu(courses: list[Course]) -> str:
    while True:
        print()
        print("要做什么？")
        for i, (label, _) in enumerate(_MENU, 1):
            print(f"  [{i}] {label}")
        print()
        try:
            raw = input("> ").strip()
        except EOFError:
            raise Cancelled("输入已结束") from None
        if not raw:
            continue
        if raw.lower() in ("q", "quit", "exit", "0"):
            raise Cancelled()
        try:
            idx = int(raw)
        except ValueError:
            print(f"  请输入 1~{len(_MENU)} 之间的编号")
            continue
        if not 1 <= idx <= len(_MENU):
            print(f"  编号范围是 1~{len(_MENU)}")
            continue

        action = _MENU[idx - 1][1]
        if action == "list":
            _print_courses(courses)
            continue
        return action


def _print_courses(courses: list[Course]) -> None:
    print()
    print(f"共 {len(courses)} 门课程：")
    for course in courses:
        semester = f"  {course.semester}" if course.semester else ""
        print(f"  · {course.name}{semester}")
    print()
    input("回车返回主菜单…")


# --------------------------------------------------------------------------
# 各步提问
# --------------------------------------------------------------------------
def _ask_scope(courses: list[Course]) -> tuple[list[Course], str]:
    if not courses:
        raise Cancelled("账号下没有可访问的课程")

    current = _current_semester(courses)
    current_set = [c for c in courses if current and c.semester == current]

    options: list[tuple[str, str]] = []
    if current_set:
        options.append((f"本学期（{current}）· {len(current_set)} 门", "current"))
    options.append((f"全部课程 · {len(courses)} 门", "all"))
    options.append(("手动挑选", "pick"))

    idx = _choose("选择课程范围：", [label for label, _ in options])
    action = options[idx][1]

    if action == "quit":
        raise Cancelled()
    if action == "current":
        return current_set, f"本学期（{current}）"
    if action == "all":
        return list(courses), "全部课程"
    return _ask_manual(courses)


def _ask_manual(courses: list[Course]) -> tuple[list[Course], str]:
    print()
    print(f"共 {len(courses)} 门课程：")
    for i, course in enumerate(courses, 1):
        semester = f"  {course.semester}" if course.semester else ""
        print(f"  [{i:>2}] {course.name}{semester}")

    print()
    print("输入编号，多个用逗号，支持区间（如 1,3,5-8）；a 全选，回车返回上一步")
    while True:
        try:
            raw = input("> ").strip()
        except EOFError:
            raise Cancelled("输入已结束") from None
        if not raw:
            return _ask_scope(courses)
        picked = parse_picks(raw, len(courses))
        if picked:
            chosen = [courses[i] for i in picked]
            return chosen, f"手动选择 {len(chosen)} 门"
        print("  没有选中任何课程，请重新输入")


def _ask_files(tasks: list[DocTask]) -> list[DocTask]:
    """列出扫描到的文件，支持编号选择或关键字过滤。"""
    print()
    if not tasks:
        print("没有找到可下载的文件。")
        return []

    view = list(tasks)
    _print_files(view)

    print()
    print("输入编号选择（如 1,3,5-8）；a 全选；n 全不选；回车=全选")
    print("也可以直接输关键字过滤，如 ch3、图形学、实验；r 恢复全部")
    while True:
        try:
            raw = input("> ").strip()
        except EOFError:
            raise Cancelled("输入已结束") from None
        lowered = raw.lower()

        if lowered in ("", "a", "all"):
            print(f"  → 已选 {len(view)} 个文件")
            return list(view)
        if lowered in ("n", "none"):
            print("  → 一个都没选")
            return []
        if lowered in ("r", "reset"):
            view = list(tasks)
            _print_files(view)
            continue

        picked = parse_picks(raw, len(view))
        if picked:
            print(f"  → 已选 {len(picked)} 个文件")
            return [view[i] for i in picked]

        matched = [
            t
            for t in tasks
            if lowered in t.attachment.name.lower() or lowered in t.course.name.lower()
        ]
        if matched:
            view = matched
            print()
            print(f"匹配「{raw}」的文件 {len(view)} 个：")
            _print_files(view, show_total=False)
            print("继续输入编号选择，或 r 恢复全部")
        else:
            print(f"  没有匹配「{raw}」的文件，换个关键字试试")


def _print_files(tasks: list[DocTask], show_total: bool = True) -> None:
    if show_total:
        total_mb = sum(t.size_hint or 0 for t in tasks) / 1048576
        print(f"共 {len(tasks)} 个文件，合计约 {total_mb:.1f} MB：")
        print()

    restricted = 0
    for i, task in enumerate(tasks, start=1):
        size = task.size_hint or 0
        if size >= 1048576:
            size_text = f"{size / 1048576:>6.1f} MB"
        elif size > 0:
            size_text = f"{size / 1024:>6.0f} KB"
        else:
            size_text = " " * 8
        mark = "  ★" if task.restricted else ""
        if task.restricted:
            restricted += 1
        print(
            f"  [{i:>3}] {size_text}  {task.course.name} / {task.attachment.name}{mark}"
        )

    if restricted:
        print()
        print(f"  ★ 标记的 {restricted} 个文件平台未开放下载，能不能取到看平台是否放行")


def _ask_confirm(
    courses: list[Course],
    scope_label: str,
    do_docs: bool,
    do_notices: bool,
    file_count: int = 0,
    notice_count: int = 0,
) -> None:
    plan = []
    if do_docs:
        plan.append(f"下载文档（{file_count} 个文件）" if file_count else "下载文档")
    if do_notices:
        plan.append(f"汇总通知（{notice_count} 门课程）" if notice_count else "汇总通知")

    print()
    print("-" * 46)
    print(f"  课程　{scope_label}（{len(courses)} 门）")
    print(f"  任务　{'、'.join(plan)}")
    print("-" * 46)
    if not _yes("开始执行？", default=True):
        raise Cancelled()


# --------------------------------------------------------------------------
# 输入解析
# --------------------------------------------------------------------------
def parse_picks(text: str, count: int) -> list[int]:
    """把 "1,3,5-8" 解析成 0 基下标列表。"""
    text = text.strip().lower().replace("，", ",")
    if text in ("a", "all", "*"):
        return list(range(count))
    if text in ("n", "none"):
        return []

    picked: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _, high = chunk.partition("-")
            try:
                start, end = int(low), int(high)
            except ValueError:
                continue
            picked.extend(range(start - 1, end))
        else:
            try:
                picked.append(int(chunk) - 1)
            except ValueError:
                continue
    return sorted({i for i in picked if 0 <= i < count})


def _choose(header: str, options: list[str], default: int = 1) -> int:
    print()
    print(header)
    for i, label in enumerate(options, 1):
        suffix = "  ← 默认" if i == default else ""
        print(f"  [{i}] {label}{suffix}")
    while True:
        try:
            raw = input("> ").strip()
        except EOFError:
            raise Cancelled("输入已结束") from None
        if not raw:
            return default - 1
        if raw.lower() in ("q", "quit", "exit"):
            raise Cancelled()
        try:
            idx = int(raw)
        except ValueError:
            print(f"  请输入 1~{len(options)} 之间的编号，或回车用默认项")
            continue
        if 1 <= idx <= len(options):
            return idx - 1
        print(f"  编号范围是 1~{len(options)}，请重新输入")


def _yes(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"{prompt} {hint} > ").strip().lower()
    except EOFError:
        raise Cancelled("输入已结束") from None
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1")


def _header(who: str) -> None:
    print()
    print("=" * 46)
    print("  畅课归档 · 交互模式")
    print("=" * 46)
    if who:
        print(f"  当前账号：{who}")
    print("  全程只读，不会修改平台上的任何内容")


def _current_semester(courses: list[Course]) -> str:
    """判断「本学期」。

    不能按课程数量投票——某学期组多门选课就赢了，会选到上学期。
    优先用开课日期：取已经开课（start_date <= 今天）里最近的那个学期；
    若全是未来课程（下学期刚放出来），取最早的那个。
    拿不到日期时退回学期名字典序，"2026-2027 第1学期" 天然大于 "2025-2026 第2学期"。
    """
    dated = [c for c in courses if c.start_date and c.semester]
    if dated:
        today = datetime.now()
        started = [c for c in dated if c.start_date <= today]  # type: ignore[operator]
        pool = started or dated
        picker = max if started else min
        return picker(pool, key=lambda c: c.start_date).semester  # type: ignore[arg-type,return-value]

    named = [c.semester for c in courses if c.semester]
    return max(named) if named else ""
