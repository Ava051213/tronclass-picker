"""畅课接口封装。

只发 GET，不碰任何写接口。所有函数返回朴素 dict，不再定义数据类——
采集层拿到什么就用什么，少一层转换。

不同学校租户字段命名有差异，取值统一走 pick()，取不到退化空值。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterator

from .client import ApiError, HttpClient, TronClassError

log = logging.getLogger("api")


def pick(data: Any, *keys: str, default: Any = None) -> Any:
    """按顺序取第一个非空字段。"""
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def as_list(data: Any, *keys: str) -> list[dict]:
    """把各种包裹形态的列表统一掏出来。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        for key in ("data", "items", "results", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                found = as_list(value, *keys)
                if found:
                    return found
    return []


def to_local(value: Any) -> datetime | None:
    """兼容 ISO 串、'YYYY-MM-DD HH:MM:SS'、毫秒/秒时间戳，统一成本地 naive 时间。"""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return to_local(int(text))
    normalized = text.replace("/", "-").replace("T", " ")
    utc = normalized.endswith("Z")
    normalized = normalized.rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        if utc:
            parsed = parsed.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        return parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def semester_of(item: dict) -> str:
    """深大租户 semester.name 常年为 null，退到 code 尾号 + 学年。"""
    academic = item.get("academic_year")
    year = ""
    if isinstance(academic, dict):
        year = str(pick(academic, "name", "code", default="") or "")
    elif academic:
        year = str(academic)

    semester = item.get("semester")
    if isinstance(semester, dict):
        readable = pick(semester, "real_name", "name")
        if readable:
            return str(readable)
        code = str(pick(semester, "code", default="") or "")
        matched = re.search(r"-(\d+)$", code)
        if year and matched:
            return f"{year} 第{matched.group(1)}学期"
    elif semester:
        return str(semester)
    return year


def truthy(value: Any) -> bool:
    """平台用 true / "true" / 1 表示允许下载，缺省视为允许。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


_DOC_TYPES = {
    "material", "document", "file", "courseware", "attachment",
    "resource", "reading", "ppt", "pdf", "video", "audio", "web_link",
    "homework", "assignment",
}


class Api:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.base = http.base_url

    # ---- 会话 --------------------------------------------------------
    def profile(self) -> dict:
        for path in ("/api/user/profile", "/api/profile", "/api/user"):
            try:
                data = self.http.get_json(path)
            except ApiError as exc:
                if exc.status in (404, 405):
                    continue
                raise
            if isinstance(data, dict) and data:
                return data
        return {}

    def courses(self) -> list[dict]:
        data = self.http.get_json("/api/my-courses")
        out = []
        for item in as_list(data, "courses", "my_courses"):
            cid = pick(item, "id", "course_id", "courseId")
            if cid is None:
                continue
            out.append({
                "id": str(cid),
                "name": str(pick(item, "name", "title", "course_name", default="未命名课程")),
                "code": str(pick(item, "code", "course_code", default="")),
                "semester": semester_of(item),
                "start": to_local(pick(item, "start_date", "start_at", "opened_at")),
            })
        log.debug("课程 %d 门", len(out))
        return out

    # ---- 活动与附件 --------------------------------------------------
    def activities(self, course_id: str) -> list[dict]:
        try:
            data = self.http.get_json(
                f"/api/courses/{course_id}/activities",
                {"sub_course_id": "0", "no-intercept": "true"},
            )
        except ApiError as exc:
            if exc.status in (403, 404):
                return []
            raise
        return as_list(data, "activities")

    def coursewares(self, course_id: str) -> list[dict]:
        """课件列表。返回体里键名多是 activities（material 类活动）。"""
        try:
            data = self.http.get_json(f"/api/course/{course_id}/coursewares")
        except ApiError as exc:
            if exc.status not in (400, 404, 422):
                raise
            data = {}
        items = as_list(data, "coursewares", "courseware", "activities")
        if items:
            return items
        try:
            cond = json.dumps({"page_size": 100, "page": 1}, separators=(",", ":"))
            data = self.http.get_json(
                f"/api/course/{course_id}/coursewares", {"conditions": cond}
            )
        except TronClassError as exc:
            log.debug("coursewares 回退失败：%s", exc)
            return []
        return as_list(data, "coursewares", "courseware", "activities")

    def uploads_of_activity(self, activity_id: str) -> list[dict]:
        try:
            data = self.http.get_json(f"/api/activities/{activity_id}/upload_references")
        except ApiError as exc:
            if exc.status in (403, 404):
                return []
            raise
        return as_list(data, "references", "attachments", "uploads", "files")

    def attachments(self, course_id: str) -> Iterator[dict]:
        """遍历一门课的全部附件，按 ref 去重。

        两个来源：课程活动（含作业、资料）和课件列表；同一份文件常挂多处。
        """
        seen: set[str] = set()

        def emit(upload: dict, source: str) -> Iterator[dict]:
            att = self._to_attachment(upload)
            if not att or att["ref"] in seen:
                return
            seen.add(att["ref"])
            att["source"] = source
            yield att

        for act in self.activities(course_id):
            title = str(pick(act, "title", "name", default=""))
            # 活动响应里给了 uploads 就以它为准：空数组说明真没附件，不必再问一次
            if "uploads" in act:
                for up in act.get("uploads") or []:
                    if isinstance(up, dict):
                        yield from emit(up, title or "课程活动")
                continue
            atype = str(pick(act, "type", "activity_type", default="")).lower()
            if atype and atype not in _DOC_TYPES:
                continue
            aid = pick(act, "id", "activity_id")
            if aid is None:
                continue
            for up in self.uploads_of_activity(str(aid)):
                yield from emit(up, title or "课程活动")

        for item in self.coursewares(course_id):
            title = str(pick(item, "name", "title", default=""))
            for key in ("uploads", "references", "attachments", "files"):
                block = item.get(key)
                if isinstance(block, list) and block:
                    for up in block:
                        if isinstance(up, dict):
                            yield from emit(up, title or "课件")
                    break
            else:
                yield from emit(item, title or "课件")

    @staticmethod
    def _to_attachment(upload: dict) -> dict | None:
        name = pick(upload, "name", "filename", "file_name", "title")
        if not name:
            return None
        ref = pick(upload, "reference_id", "referenceId", "id", "upload_id")
        if ref is None:
            return None
        upload_id = pick(upload, "id", "upload_id")
        created = to_local(
            pick(upload, "created_at", "referenced_at", "updated_at", "uploaded_at")
        )
        return {
            "ref": str(ref),
            "upload_id": str(upload_id or ""),
            "name": str(name),
            "date": created.strftime("%m-%d") if created else "",
            "size": as_int(pick(upload, "size", "file_size", "bytes", default=0)),
            "mime": str(pick(upload, "type", "mime", default="") or "").lower(),
            "allow": truthy(upload.get("allow_download")) and truthy(
                upload.get("origin_allow_download")
            ),
            "url": str(pick(upload, "url", "download_url", "link", default="") or ""),
            "raw": upload,
        }

    # ---- 取文件地址 --------------------------------------------------
    def download_url(self, att: dict) -> str:
        """正式下载地址。平台标了禁止下载时这里通常返回 403。"""
        if att["url"].startswith("http"):
            return att["url"]

        candidates = []
        if att["upload_id"]:
            candidates.append(f"/api/uploads/{att['upload_id']}/url")
        candidates.append(f"/api/uploads/reference/document/{att['ref']}/url")
        if att["upload_id"] and att["upload_id"] != att["ref"]:
            candidates.append(f"/api/uploads/{att['ref']}/url")

        last: Exception | None = None
        for path in candidates:
            try:
                data = self.http.get_json(path)
            except ApiError as exc:
                last = exc
                if exc.status in (400, 403, 404, 405, 422):
                    continue
                raise
            url = pick(data, "url", "download_url", "signed_url", "link", default="")
            if not url and isinstance(data, dict):
                inner = data.get("data") or data.get("result")
                if isinstance(inner, dict):
                    url = pick(inner, "url", "download_url")
            if url:
                return str(url)
        raise TronClassError(f"取不到下载地址：{att['name']}（{last}）")

    def preview_url(self, att: dict) -> str:
        """旧版遗留接口，保留占位。实测禁下载文件的签名地址照样能取，无需走这里。"""
        raise TronClassError("预览流已废弃，禁下载文件直接走 download_url")

    # ---- 待办 --------------------------------------------------------
    def todos(self, strict: bool = False) -> list[dict]:
        """待办清单。end_time 是 UTC，这里统一成本地时间。

        strict=True 时把异常抛出去，让外部接口能区分「真没作业」和「没取到」，
        别把登录失效当成零待办。
        """
        try:
            data = self.http.get_json("/api/todos")
        except TronClassError as exc:
            if strict:
                raise
            log.debug("待办读取失败：%s", exc)
            return []

        out = []
        for item in as_list(data, "todo_list", "todos"):
            due = to_local(pick(item, "end_time", "end_at", "deadline"))
            out.append({
                "id": str(pick(item, "id", "activity_id", default="")),
                "title": str(pick(item, "title", "name", default="未命名待办")),
                "course": str(pick(item, "course_name", default="")),
                "course_id": str(pick(item, "course_id", default="")),
                "type": str(pick(item, "type", default="")),
                "due": due.strftime("%Y-%m-%d %H:%M") if due else "",
                "due_ts": due.strftime("%Y-%m-%dT%H:%M:%S") if due else "",
            })
        out.sort(key=lambda t: t["due_ts"] or "9999")
        return out

    # ---- 消息 --------------------------------------------------------
    def notices(self, courses: list[dict], per_course: int = 30) -> list[dict]:
        out: list[dict] = []

        try:
            data = self.http.get_json("/api/announcement", {"page": 1, "page_size": 50})
            for item in as_list(data, "announcements", "announcement"):
                n = self._to_notice(item, "平台公告", "")
                if n:
                    out.append(n)
        except TronClassError as exc:
            log.debug("平台公告读取失败：%s", exc)

        for course in courses:
            for path in (
                f"/api/courses/{course['id']}/bulletins",
                f"/api/course/{course['id']}/bulletins",
            ):
                try:
                    data = self.http.get_json(
                        path, {"page": 1, "page_size": per_course, "no-intercept": "true"}
                    )
                except ApiError as exc:
                    if exc.status in (403, 404, 405):
                        continue
                    raise
                for item in as_list(data, "bulletins", "announcements", "announcement"):
                    n = self._to_notice(item, course["name"], course["id"])
                    if n:
                        out.append(n)
                break

        out.sort(key=lambda n: n["at"] or "", reverse=True)
        return out

    def notice_body(self, notice: dict) -> str:
        if notice.get("body"):
            return notice["body"]
        for path in (f"/api/announcements/{notice['id']}", f"/api/announcement/{notice['id']}"):
            try:
                data = self.http.get_json(path)
            except ApiError as exc:
                if exc.status in (403, 404):
                    continue
                raise
            body = pick(data, "content", "body", "description", "text", default="")
            if body:
                return str(body)
        return ""

    @staticmethod
    def _to_notice(item: dict, course: str, course_id: str) -> dict | None:
        nid = pick(item, "id", "announcement_id")
        title = pick(item, "title", "name", "subject")
        if nid is None or not title:
            return None
        at = to_local(pick(item, "published_at", "publish_time", "created_at", "start_time"))
        return {
            "id": str(nid),
            "title": str(title),
            "course": course,
            "course_id": course_id,
            "at": at.strftime("%Y-%m-%dT%H:%M:%S") if at else "",
            "time": at.strftime("%m-%d %H:%M") if at else "",
            "body": str(pick(item, "content", "body", "description", default="")),
        }
