"""领域模型：贯穿采集、规划、执行三层的共同语言。

这里只放数据，不放行为。所有字段都在构造时确定，便于比较和测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------
# 平台对象
# --------------------------------------------------------------------------
@dataclass
class Course:
    id: str
    name: str
    code: str = ""
    semester: str = ""
    department: str = ""
    role: str = ""
    start_date: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        return f"{self.name}（{self.code}）" if self.code else self.name


@dataclass
class Activity:
    id: str
    course_id: str
    title: str
    type: str = ""
    module: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Attachment:
    """一份可下载文件的引用。"""
    ref_id: str
    name: str
    size: int = 0
    activity_id: str = ""
    upload_id: str = ""
    url_hint: str = ""
    kind: str = ""
    allow_download: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Announcement:
    id: str
    title: str
    course: str = ""
    course_id: str = ""
    author: str = ""
    published_at: datetime | None = None
    content: str = ""
    url: str = ""
    scope: str = "course"          # platform | course
    attachment_count: int = 0

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.course_id}:{self.id}"


# --------------------------------------------------------------------------
# 任务与计划
# --------------------------------------------------------------------------
@dataclass
class DocTask:
    """一个待归档的文件：来源、落点、去重键都已确定。"""
    key: str
    course: Course
    attachment: Attachment
    target: Path
    source: str = ""
    size_hint: int = 0
    restricted: bool = False        # 平台标记了 allow_download=false


@dataclass
class Selection:
    """用户意图——「这次要处理哪些课、哪些文件、做哪几件事」。

    命令行和向导都产出一个 Selection，后续流程不再关心它从哪来。
    tasks 为 None 表示交由规划器扫描决定；给定列表表示用户已逐个挑好。
    """
    courses: list[Course]
    want_documents: bool = True
    want_notices: bool = True
    tasks: list[DocTask] | None = None
    extensions: list[str] | None = None
    label: str = ""
    notices_follow: bool = True    # False 表示通知按配置全量扫，不跟随 courses
    include_restricted: bool = False   # 是否纳入 allow_download=false 的文件

    @property
    def course_ids(self) -> set[str]:
        return {c.id for c in self.courses}


@dataclass
class ScanResult:
    """扫描课程产出的文件清单。

    tasks 里可能含 restricted=True 的条目，是否使用由调用方决定；
    restricted_count 便于上层提示「还有几个文件被老师关了下载」。
    """
    tasks: list[DocTask] = field(default_factory=list)
    restricted_count: int = 0


@dataclass
class Failure:
    name: str
    reason: str


@dataclass
class Job:
    """一次执行计划：要下哪些文件、要扫哪些课的通知。"""
    selection: Selection
    doc_tasks: list[DocTask] = field(default_factory=list)
    notice_courses: list[Course] = field(default_factory=list)
    blocked: int = 0                       # 被平台标记为禁止下载而跳过的数量

    @property
    def empty(self) -> bool:
        return not self.doc_tasks and not self.notice_courses


@dataclass
class JobResult:
    """执行结果，供报告层消费。"""
    planned_files: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: list[Failure] = field(default_factory=list)
    bytes: int = 0

    notice_total: int = 0
    notice_new: int = 0
    notice_by_scope: dict[str, int] = field(default_factory=dict)
    notice_file: Path | None = None

    elapsed: float = 0.0
    requests: int = 0
    retries: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed
