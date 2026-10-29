"""应用层：组装依赖，对外只暴露四个动作——连接、扫描、规划、执行。

命令行入口和交互向导都只调这里，不再各自拼装流程。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .api import TronClassAPI
from .client import HttpClient
from .config import Config
from .docs import DocumentDownloader, DocumentScanner
from .models import Course, Failure, Job, JobResult, ScanResult, Selection
from .notices import NoticeCollector
from .planning import Planner
from .state import StateStore

log = logging.getLogger("app")


class App:
    """一次运行的全部上下文，避免流程里到处 new 对象、到处传参。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.http = HttpClient(cfg)
        self.api = TronClassAPI(self.http)
        self.state = StateStore(cfg.state_file)
        self.scanner = DocumentScanner(cfg, self.api)
        self.planner = Planner(cfg, self.scanner)
        self._courses: list[Course] = []

    # ---- 采集 --------------------------------------------------------
    def connect(self) -> tuple[str, list[Course]]:
        """验证会话并拉课程列表。会话失效时抛 AuthExpired。"""
        profile = self.api.check_session()
        self._courses = self.api.my_courses()
        return _display_name(profile), self._courses

    def scan(self, courses: list[Course], extensions: list[str] | None = None) -> ScanResult:
        return self.scanner.scan(courses, extensions)

    # ---- 规划 --------------------------------------------------------
    def plan(self, selection: Selection) -> Job:
        return self.planner.build(selection, self._courses)

    # ---- 执行 --------------------------------------------------------
    def run(self, job: Job, dry_run: bool = False, progress=None) -> JobResult:
        started = time.monotonic()
        result = JobResult(planned_files=len(job.doc_tasks))

        if job.selection.want_documents:
            if job.doc_tasks:
                downloader = DocumentDownloader(self.cfg, self.http, self.api, self.state)
                summary = downloader.run(job.doc_tasks, dry_run=dry_run, progress=progress)
                result.downloaded = summary.downloaded
                result.skipped = summary.skipped
                result.bytes = summary.bytes
                result.failed = [Failure(name, reason) for name, reason in summary.failures]
                result.success_keys = summary.success_keys
            else:
                log.info("没有匹配的可下载文件")

        if job.notice_courses:
            collector = NoticeCollector(self.cfg, self.api, self.state)
            items = collector.collect(job.notice_courses)
            result.notice_items = items
            notice_summary = collector.write(items)
            result.notice_total = notice_summary.total
            result.notice_new = notice_summary.new
            result.notice_by_scope = dict(notice_summary.by_scope)
            result.notice_file = notice_summary.file
        elif job.selection.want_notices:
            log.info("没有需要扫描通知的课程")

        result.elapsed = time.monotonic() - started
        stats = self.http.stats()
        result.requests = stats["requests"]
        result.retries = stats["retries"]
        return result

    @property
    def sessions(self) -> str:
        return self.http.describe_session()


def _display_name(profile: Any) -> str:
    if not isinstance(profile, dict):
        return ""
    return str(
        profile.get("name")
        or profile.get("display_name")
        or profile.get("username")
        or profile.get("login")
        or ""
    )
