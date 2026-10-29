"""决策层：把「用户选了什么」翻译成「具体要干哪些事」。

这一层不碰网络、不写磁盘，输入输出都是数据类，便于单测。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import Config
from .models import Course, Job, ScanResult, Selection

log = logging.getLogger("plan")

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------------
# 命名与落点
# --------------------------------------------------------------------------
def safe_filename(name: str, max_length: int = 100) -> str:
    """清洗成 Windows/macOS 都能落盘的文件名。"""
    name = name.replace("\u3000", " ").strip().strip(".")
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "未命名"

    stem, dot, suffix = name.rpartition(".")
    if not dot or len(suffix) > 12:
        stem, suffix = name, ""

    if stem.lower() in _RESERVED:
        stem = f"_{stem}"

    keep = max_length - (len(suffix) + 1 if suffix else 0)
    return f"{stem[:keep].rstrip()}.{suffix}" if suffix else stem[:keep].rstrip()


def guess_extension(name: str, fallback: str = "") -> str:
    suffix = Path(name).suffix.lower()
    if suffix and len(suffix) <= 12:
        return suffix
    return fallback.lower() or ".bin"


def resolve_target(
    cfg: Config, course: Course, filename: str, used: set[str]
) -> Path:
    """算出文件该落在哪，并处理同名冲突。used 会被就地更新。"""
    root = cfg.documents_dir
    safe_course = safe_filename(course.name, 60)
    organize = cfg.documents.organize_by

    if organize == "semester":
        semester = safe_filename(course.semester or "未标注学期", 40)
        folder = root / semester / safe_course
    elif organize == "flat":
        folder = root
        filename = f"{safe_course}_{filename}"
    else:
        folder = root / safe_course

    base = folder / safe_filename(filename)
    candidate = base
    counter = 2
    while str(candidate).lower() in used:
        candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        counter += 1
    used.add(str(candidate).lower())
    return candidate


# --------------------------------------------------------------------------
# 课程筛选
# --------------------------------------------------------------------------
def _matches(course: Course, keywords: list[str]) -> bool:
    haystack = f"{course.name} {course.code} {course.id}".lower()
    return any(k.lower() in haystack for k in keywords if k.strip())


def select_courses(
    courses: list[Course],
    cfg: Config,
    *,
    scope: bool = True,
    extra_include: list[str] | None = None,
) -> list[Course]:
    """按配置挑课程。

    scope=True 走完整筛选（学期 + 关键词），用于文档下载。
    scope=False 只应用 exclude，用于通知这种要全量扫的场景。
    exclude 永远生效。
    """
    result = courses

    if scope and cfg.courses.semester:
        needle = cfg.courses.semester.lower()
        filtered = [c for c in result if needle in c.semester.lower()]
        if filtered:
            result = filtered
        elif result:
            log.warning("没有课程匹配学期关键字「%s」，已忽略该筛选", cfg.courses.semester)

    include = list(cfg.courses.include) + list(extra_include or [])
    if scope and include:
        result = [c for c in result if _matches(c, include)]

    if cfg.courses.exclude:
        result = [c for c in result if not _matches(c, cfg.courses.exclude)]
    return result


# --------------------------------------------------------------------------
# 规划器
# --------------------------------------------------------------------------
class Planner:
    """把 Selection 变成 Job。扫描动作通过 scanner 注入，保持本类可测。"""

    def __init__(self, cfg: Config, scanner=None) -> None:
        self.cfg = cfg
        self.scanner = scanner

    def build(self, selection: Selection, all_courses: list[Course]) -> Job:
        tasks: list = []
        blocked = 0

        if selection.want_documents:
            if selection.tasks is not None:
                # 用户已经逐个挑过，尊重他的选择
                tasks = list(selection.tasks)
            elif self.scanner is not None:
                result: ScanResult = self.scanner.scan(selection.courses)
                tasks = result.tasks
                if not selection.include_restricted:
                    kept = [t for t in tasks if not t.restricted]
                    blocked = len(tasks) - len(kept)
                    tasks = kept

        if not selection.want_notices:
            notice_courses: list[Course] = []
        elif selection.notices_follow:
            notice_courses = list(selection.courses)
        else:
            notice_courses = select_courses(all_courses, self.cfg, scope=False)

        return Job(
            selection=selection,
            doc_tasks=tasks,
            notice_courses=notice_courses,
            blocked=blocked,
        )
