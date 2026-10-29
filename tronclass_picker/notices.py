"""课程通知 / 公告的收集与 Markdown 汇总。"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from .api import Announcement, Course, TronClassAPI
from .client import AuthExpired, TronClassError
from .config import Config
from .state import StateStore

log = logging.getLogger("notices")

_MAX_BODY_CHARS = 1600


@dataclass
class NoticeSummary:
    total: int = 0
    new: int = 0
    by_scope: dict[str, int] = field(default_factory=dict)
    file: Path | None = None


class _TextExtractor(HTMLParser):
    """把公告正文的 HTML 压成可读纯文本。"""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("· ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    if "<" not in raw:
        return html.unescape(raw).strip()
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw).strip()
    return parser.text()


class NoticeCollector:
    def __init__(self, cfg: Config, api: TronClassAPI, state: StateStore) -> None:
        self.cfg = cfg
        self.api = api
        self.base_url = api.base_url
        self.state = state
        self.since = (
            datetime.now() - timedelta(days=cfg.notices.days) if cfg.notices.days else None
        )

    # ---- 收集 --------------------------------------------------------
    def collect(self, courses: Iterable[Course]) -> list[Announcement]:
        collected: list[Announcement] = []

        if self.cfg.notices.include_platform:
            collected.extend(self._platform())

        if self.cfg.notices.include_course:
            # 几十门课的公告接口串行会很慢，并发几个；实际发送仍受 http 层限速约束
            course_list = list(courses)
            if course_list:
                workers = min(4, len(course_list))
                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="notice"
                ) as pool:
                    futures = {pool.submit(self._course_ann, c): c for c in course_list}
                    for future in as_completed(futures):
                        course = futures[future]
                        try:
                            items = future.result()
                        except AuthExpired:
                            for pending in futures:
                                pending.cancel()
                            raise
                        except TronClassError as exc:
                            log.warning("读取 %s 的课程通知失败：%s", course.name, exc)
                            continue
                        for ann in items:
                            if not ann.course:
                                ann.course = course.name
                            if not ann.course_id:
                                ann.course_id = course.id
                        collected.extend(items)

        log.info("共采集到 %d 条原始通知", len(collected))
        return self._filter(collected)

    def _course_ann(self, course: Course) -> list[Announcement]:
        return self.api.course_announcements(course, limit=self.cfg.notices.max_per_course)

    def _platform(self) -> list[Announcement]:
        results: list[Announcement] = []
        page, page_size, max_pages = 1, 50, 20
        while page <= max_pages:
            try:
                items, total = self.api.platform_announcements(page, page_size)
            except AuthExpired:
                raise
            except TronClassError as exc:
                log.warning("读取平台公告第 %d 页失败：%s", page, exc)
                break
            if not items:
                break
            results.extend(items)
            if total and len(results) >= total:
                break
            if self.since and all(
                a.published_at and a.published_at < self.since for a in items
            ):
                break
            if len(items) < page_size:
                break
            page += 1
        log.info("平台公告：%d 条", len(results))
        return results

    def _filter(self, items: list[Announcement]) -> list[Announcement]:
        unique: dict[str, Announcement] = {}
        for ann in items:
            key = self._key(ann)
            if key in unique:
                continue
            if self.since and ann.published_at and ann.published_at < self.since:
                continue
            unique[key] = ann
        ordered = sorted(
            unique.values(),
            key=lambda a: a.published_at or datetime.min,
            reverse=True,
        )
        return ordered

    @staticmethod
    def _key(ann: Announcement) -> str:
        return f"{ann.scope}:{ann.course_id}:{ann.id}"

    # ---- 输出 --------------------------------------------------------
    def write(self, items: list[Announcement]) -> NoticeSummary:
        summary = NoticeSummary(total=len(items))
        fresh = [a for a in items if not self.state.notice_seen(self._key(a))]
        summary.new = len(fresh)

        if not fresh:
            log.info("没有新通知（已汇总过 %d 条）", len(items))
            return summary

        if self.cfg.notices.include_content:
            self._enrich(fresh)

        # 先进日志，保证 output=log 时也能看到内容
        self._log_each(fresh)

        mode = (self.cfg.notices.output or "both").lower()
        if mode in ("markdown", "both"):
            self.cfg.notices_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now()
            target = self.cfg.notices_dir / f"{stamp:%Y-%m-%d_%H%M}_通知汇总.md"
            target.write_text(self._render(fresh, stamp), encoding="utf-8")

            latest = self.cfg.notices_dir / "latest.md"
            latest.write_text(
                f"最新一期：{target.name}\n\n" + target.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            summary.file = target

        for ann in fresh:
            self.state.mark_notice(self._key(ann))
        self.state.save()

        for ann in fresh:
            summary.by_scope[ann.scope] = summary.by_scope.get(ann.scope, 0) + 1
        return summary

    @staticmethod
    def _log_each(items: list[Announcement]) -> None:
        """把新增通知逐条写进日志，便于每天翻一眼。"""
        log.info("本次新增 %d 条通知：", len(items))
        for ann in items:
            where = ann.course or "平台公告"
            when = f"{ann.published_at:%m-%d %H:%M}" if ann.published_at else "时间未知"
            log.info("  · [%s] %s（%s）", where, ann.title, when)
            body = html_to_text(ann.content).replace("\n", " ")
            if body:
                snippet = body[:120] + ("…" if len(body) > 120 else "")
                log.info("      %s", snippet)

    def _enrich(self, items: list[Announcement]) -> None:
        for ann in items:
            if ann.content:
                continue
            try:
                ann.content = self.api.announcement_detail(ann)
            except AuthExpired:
                raise
            except TronClassError as exc:
                log.debug("拉取公告正文失败 %s：%s", ann.title, exc)

    def _render(self, items: list[Announcement], stamp: datetime) -> str:
        lines: list[str] = []
        scope_name = {"platform": "平台公告", "course": "课程通知"}

        lines.append(f"# 畅课通知汇总 · {stamp:%Y-%m-%d}")
        lines.append("")
        window = f"最近 {self.cfg.notices.days} 天" if self.cfg.notices.days else "全部历史"
        lines.append(
            f"生成时间：{stamp:%Y-%m-%d %H:%M}　|　覆盖范围：{window}　|　"
            f"本次新增 {len(items)} 条"
        )
        lines.append("")
        lines.append("---")
        lines.append("")

        grouped: dict[str, list[Announcement]] = {}
        for ann in items:
            grouped.setdefault(ann.scope, []).append(ann)

        index = 1
        for scope in ("platform", "course"):
            bucket = grouped.get(scope)
            if not bucket:
                continue
            lines.append(f"## {_cn_num(index)}、{scope_name[scope]}")
            lines.append("")
            index += 1

            if scope == "course":
                by_course: dict[str, list[Announcement]] = {}
                for ann in bucket:
                    by_course.setdefault(ann.course or "未归属课程", []).append(ann)
                for course_name, group in by_course.items():
                    lines.append(f"### {course_name}（{len(group)} 条）")
                    lines.append("")
                    for ann in group:
                        lines.extend(self._render_one(ann, 4, self._link_of(ann)))
            else:
                for ann in bucket:
                    lines.extend(self._render_one(ann, 3, self._link_of(ann)))

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(
            "> 本文件由 tronclass-picker 自动生成，仅用于个人学习资料归档；"
            "请勿传播或用于平台规范不允许的用途。"
        )
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"

    @staticmethod
    def _render_one(ann: Announcement, level: int, link: str = "") -> list[str]:
        hashes = "#" * level
        lines = [f"{hashes} {_clean_title(ann.title)}", ""]

        meta = []
        if ann.published_at:
            meta.append(f"发布：{ann.published_at:%Y-%m-%d %H:%M}")
        if ann.author:
            meta.append(f"发布人：{ann.author}")
        if ann.attachment_count:
            meta.append(f"附件：{ann.attachment_count} 个")
        if meta:
            lines.append("　|　".join(meta))
            lines.append("")

        body = html_to_text(ann.content)
        if body:
            if len(body) > _MAX_BODY_CHARS:
                body = body[:_MAX_BODY_CHARS].rstrip() + "……（正文已截断）"
            lines.extend(body.splitlines())
            lines.append("")

        if link:
            lines.append(f"原文：{link}")
            lines.append("")

        return lines

    def _link_of(self, ann: Announcement) -> str:
        if ann.url:
            return ann.url
        if ann.scope == "platform":
            return f"{self.base_url}/announcement"
        if ann.course_id:
            return f"{self.base_url}/course/{ann.course_id}/announcement"
        return ""


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().replace("#", "＃")


def _cn_num(n: int) -> str:
    return "一二三四五六七八九十"[n - 1] if 1 <= n <= 10 else str(n)
