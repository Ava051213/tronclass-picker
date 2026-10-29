"""课程文档的扫描与下载。

扫描（课程 → 文件清单）和执行（清单 → 落盘）是两件事，分成两个类：
扫描负责发现、去重、分配落点；下载只管把字节搬下来。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable

from .api import TronClassAPI, _attachment_from_upload, _first_list, _pick
from .client import AuthExpired, HttpClient, TronClassError
from .config import DOC_ACTIVITY_TYPES, Config
from .models import Attachment, Course, DocTask, ScanResult
from .planning import guess_extension, resolve_target
from .state import StateStore

log = logging.getLogger("docs")


@dataclass
class DownloadSummary:
    total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
class DocumentScanner:
    """遍历课程，产出可下载文件清单。"""

    def __init__(self, cfg: Config, api: TronClassAPI) -> None:
        self.cfg = cfg
        self.api = api
        self._used_paths: set[str] = set()
        self._seen_keys: set[str] = set()
        self._allowed: set[str] = set()
        self._restricted = 0

    def scan(
        self, courses: Iterable[Course], extensions: list[str] | None = None
    ) -> ScanResult:
        # 每次扫描都是独立批次，状态重置，避免跨轮污染
        self._used_paths.clear()
        self._seen_keys.clear()
        self._restricted = 0
        # 类型白名单可以按本轮覆盖，不必改配置
        allowed = extensions if extensions is not None else self.cfg.documents.extensions
        self._allowed = {
            e.lower() if e.startswith(".") else f".{e.lower()}" for e in allowed
        }

        tasks: list[DocTask] = []
        for course in courses:
            before = len(tasks)
            tasks.extend(self._from_coursewares(course))
            tasks.extend(self._from_activities(course))
            log.info("· %s：%d 个文件", course.name, len(tasks) - before)

        if self._restricted:
            log.info("其中 %d 个文件被平台标记为禁止下载", self._restricted)
        return ScanResult(tasks=tasks, restricted_count=self._restricted)

    # ---- 两个来源 ----------------------------------------------------
    def _from_coursewares(self, course: Course) -> list[DocTask]:
        if not self.cfg.documents.include_coursewares:
            return []
        try:
            items = self.api.coursewares(course.id)
        except AuthExpired:
            raise
        except TronClassError as exc:
            log.warning("读取 %s 的课件列表失败：%s", course.name, exc)
            return []

        tasks = []
        for item in items:
            title = str(_pick(item, "name", "title", default="课件"))
            for att in self._attachments_of(item):
                task = self._make_task(course, title, att, source="课件")
                if task:
                    tasks.append(task)
        return tasks

    def _from_activities(self, course: Course) -> list[DocTask]:
        if not self.cfg.documents.include_activity_refs:
            return []
        try:
            activities = self.api.activities(course.id)
        except AuthExpired:
            raise
        except TronClassError as exc:
            log.warning("读取 %s 的活动列表失败：%s", course.name, exc)
            return []

        tasks = []
        for activity in activities:
            if activity.type and activity.type not in DOC_ACTIVITY_TYPES:
                continue
            try:
                attachments = self.api.attachments_of_activity(activity)
            except AuthExpired:
                raise
            except TronClassError as exc:
                log.debug("活动 %s(%s) 读取附件失败：%s", activity.title, activity.id, exc)
                continue
            for att in attachments:
                if not att.activity_id:
                    att.activity_id = activity.id
                task = self._make_task(course, activity.title, att, source="活动附件")
                if task:
                    tasks.append(task)
        return tasks

    def _attachments_of(self, item: dict[str, Any]) -> list[Attachment]:
        """从课件条目里抽出附件；条目本身是文件时也认。"""
        for key in ("uploads", "references", "attachments", "files"):
            found = [
                att
                for sub in _first_list(item, (key,))
                if (att := _attachment_from_upload(sub))
            ]
            if found:
                return found

        att = _attachment_from_upload(item)
        return [att] if att else []

    # ---- 过滤与落点 --------------------------------------------------
    def _make_task(
        self, course: Course, title: str, att: Attachment, source: str
    ) -> DocTask | None:
        # 同一份文件常被挂到多个活动下，按引用 ID 去重
        key = self._key_of(course, att)
        if key in self._seen_keys:
            return None

        ext = guess_extension(att.name)
        if self._allowed and ext not in self._allowed:
            log.debug("跳过非目标类型：%s", att.name)
            return None

        # allow_download=false 只做标记，收不收由上层决定：
        # 命令行要 --include-restricted，向导会单独问一次
        restricted = not att.allow_download
        if restricted:
            self._restricted += 1

        self._seen_keys.add(key)
        return DocTask(
            key=key,
            course=course,
            attachment=att,
            target=resolve_target(self.cfg, course, att.name, self._used_paths),
            source=f"{source}／{title}" if title else source,
            size_hint=att.size,
            restricted=restricted,
        )

    @staticmethod
    def _key_of(course: Course, att: Attachment) -> str:
        if att.ref_id:
            return f"{course.id}:{att.ref_id}"
        return f"{course.id}:{att.activity_id}:{att.name}"


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
class DocumentDownloader:
    """把 DocTask 落到磁盘，带增量跳过和并发。"""

    def __init__(
        self, cfg: Config, http: HttpClient, api: TronClassAPI, state: StateStore
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.api = api
        self.state = state

    def run(
        self,
        tasks: list[DocTask],
        dry_run: bool = False,
        progress=None,
    ) -> DownloadSummary:
        summary = DownloadSummary(total=len(tasks))
        pending: list[DocTask] = []

        for task in tasks:
            if self.cfg.documents.skip_existing and self.state.has_document(task.key):
                summary.skipped += 1
                continue
            if self.cfg.documents.skip_existing and task.target.exists():
                size = task.target.stat().st_size
                summary.skipped += 1
                self.state.mark_document(task.key, task.target, size, "")
                continue
            pending.append(task)

        if not pending:
            log.info("没有新文件需要下载（已跳过 %d 个）", summary.skipped)
            self.state.save()
            return summary

        if dry_run:
            for task in pending:
                log.info("[演练] %s → %s", task.attachment.name, task.target)
            summary.skipped += len(pending)
            return summary

        max_bytes = self.cfg.documents.max_size_mb * 1024 * 1024
        workers = min(self.cfg.documents.concurrency, len(pending))

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl") as pool:
            futures = {
                pool.submit(self._download_one, task, max_bytes): task for task in pending
            }
            for index, future in enumerate(as_completed(futures), start=1):
                task = futures[future]
                try:
                    size = future.result()
                except AuthExpired:
                    for f in futures:
                        f.cancel()
                    raise
                except TronClassError as exc:
                    summary.failed += 1
                    summary.failures.append((task.attachment.name, str(exc)))
                    log.error(
                        "[%d/%d] 失败 %s：%s", index, len(pending), task.attachment.name, exc
                    )
                except Exception as exc:  # 兜底，不让单个文件拖垮整轮
                    summary.failed += 1
                    summary.failures.append((task.attachment.name, repr(exc)))
                    log.exception("[%d/%d] 异常 %s", index, len(pending), task.attachment.name)
                else:
                    summary.downloaded += 1
                    summary.bytes += size
                    log.info(
                        "[%d/%d] ✓ %s（%.2f MB）",
                        index, len(pending), task.target.name, size / 1048576,
                    )
                if index % 20 == 0:
                    self.state.save()

                if progress is not None:
                    try:
                        progress(index, len(pending), task.attachment.name)
                    except Exception:  # 回调出错不能影响下载
                        pass

        self.state.save()
        return summary

    def _download_one(self, task: DocTask, max_bytes: int) -> int:
        url = self.api.resolve_download_url(task.attachment)
        result = self.http.download(
            url,
            task.target,
            max_bytes=max_bytes,
            referer=f"{self.cfg.base_url}/course/{task.course.id}/learning-activity",
        )
        final = _match_extension(result.path, result.content_type)
        if final != result.path:
            log.info("  平台给的是转码文件，扩展名按实际类型改成 %s", final.name)
        self.state.mark_document(task.key, final, result.size, result.sha1)
        return result.size


def _match_extension(path, content_type: str):
    """平台对受限文件常在服务端转成 PDF，扩展名会对不上，这里按实际类型纠正。"""
    ctype = (content_type or "").lower()
    if "pdf" not in ctype or path.suffix.lower() == ".pdf":
        return path

    target = path.with_suffix(".pdf")
    if target.exists():
        target = target.with_name(f"{target.stem}_1{target.suffix}")
    try:
        path.rename(target)
    except OSError:
        return path
    return target
