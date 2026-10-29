"""TronClass 业务接口封装。

只使用 GET，不触碰任何写接口（提交、上传、删改）。不同学校租户的字段
命名略有差异，这里统一做兼容取值，取不到时退化为空值而不是抛异常。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .client import ApiError, HttpClient, TronClassError
from .models import Activity, Announcement, Attachment, Course

log = logging.getLogger("api")


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _pick(data: Any, *keys: str, default: Any = None) -> Any:
    """从 dict 里按顺序取第一个非空字段。"""
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _semester_name(item: dict[str, Any]) -> str:
    """拼出可读的学期名。

    深大租户的返回形如：
        academic_year: {"name": "2026-2027"}
        semester:      {"code": "2026-1", "name": null, "real_name": null}
    name 常年为 null，所以要退到 code 的尾号 + 学年名。
    """
    academic = item.get("academic_year")
    year = ""
    if isinstance(academic, dict):
        year = str(_pick(academic, "name", "code", default="") or "")
    elif academic:
        year = str(academic)

    semester = item.get("semester")
    if isinstance(semester, dict):
        readable = _pick(semester, "real_name", "name")
        if readable:
            return str(readable)
        code = str(_pick(semester, "code", default="") or "")
        matched = re.search(r"-(\d+)$", code)
        if year and matched:
            return f"{year} 第{matched.group(1)}学期"
    elif semester:
        # 少数租户直接给字符串
        return str(semester)
    return year


def _first_list(data: Any, keys: Iterable[str]) -> list[dict[str, Any]]:
    """在返回体里找列表：既支持裸数组，也支持 {"courses": [...]} 这类包裹。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        # 有些租户把分页信息放外层，数据在 data/items/results
        for key in ("data", "items", "results", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = _first_list(value, keys)
                if nested:
                    return nested
    return []


def to_datetime(value: Any) -> datetime | None:
    """兼容 ISO 字符串、'YYYY-MM-DD HH:MM:SS'、毫秒/秒时间戳。

    统一返回不带时区的本地时间，避免和 datetime.now() 比较时抛 TypeError。
    """
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
        return to_datetime(int(text))
    normalized = text.replace("/", "-").replace("T", " ")
    is_utc = normalized.endswith("Z")
    normalized = normalized.rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        if is_utc:
            parsed = parsed.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        return parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed



# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class TronClassAPI:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.base_url = http.base_url

    # ---- 会话自检 ----------------------------------------------------
    def check_session(self) -> dict[str, Any]:
        """验证 Cookie 是否有效，返回用户信息（拿不到就返回空 dict）。"""
        for path in ("/api/user/profile", "/api/profile", "/api/user"):
            try:
                data = self.http.get_json(path)
            except ApiError as exc:
                if exc.status in (404, 405):
                    continue
                raise
            except TronClassError:
                raise
            if isinstance(data, dict) and data:
                return data
        return {}

    # ---- 课程 --------------------------------------------------------
    def my_courses(self) -> list[Course]:
        data = self.http.get_json("/api/my-courses")
        items = _first_list(data, ("courses", "my_courses"))
        courses = []
        for item in items:
            course_id = _pick(item, "id", "course_id", "courseId")
            if course_id is None:
                continue
            department = item.get("department")
            dept = department.get("name") if isinstance(department, dict) else department
            courses.append(
                Course(
                    id=str(course_id),
                    name=str(_pick(item, "name", "title", "course_name", default="未命名课程")),
                    code=str(_pick(item, "code", "course_code", default="")),
                    semester=_semester_name(item),
                    department=str(dept or ""),
                    role=str(_pick(item, "role", "user_role", default="")),
                    start_date=to_datetime(_pick(item, "start_date", "start_at", "opened_at")),
                    raw=item,
                )
            )
        log.debug("/api/my-courses 返回 %d 门课程", len(courses))
        return courses

    # ---- 课程活动 ----------------------------------------------------
    def activities(self, course_id: str, sub_course_id: str = "0") -> list[Activity]:
        data = self.http.get_json(
            f"/api/courses/{course_id}/activities",
            {"sub_course_id": sub_course_id, "no-intercept": "true"},
        )
        items = _first_list(data, ("activities",))
        result = []
        for item in items:
            activity_id = _pick(item, "id", "activity_id")
            if activity_id is None:
                continue
            result.append(
                Activity(
                    id=str(activity_id),
                    course_id=str(course_id),
                    title=str(_pick(item, "title", "name", default="未命名活动")),
                    type=str(_pick(item, "type", "activity_type", default="")).lower(),
                    module=str(_pick(item, "module_name", "module", default="")),
                    start_at=to_datetime(_pick(item, "start_time", "start_at", "created_at")),
                    end_at=to_datetime(_pick(item, "end_time", "end_at", "deadline")),
                    url=f"{self.base_url}/course/{course_id}/learning-activity#/{activity_id}",
                    raw=item,
                )
            )
        return result

    def coursewares(self, course_id: str) -> list[dict[str, Any]]:
        """课件列表。返回体里键名是 activities（material 类活动），不是 coursewares。"""
        try:
            data = self.http.get_json(f"/api/course/{course_id}/coursewares")
        except ApiError as exc:
            if exc.status not in (400, 404, 422):
                raise
            data = {}
        items = _first_list(data, ("coursewares", "courseware", "activities"))
        if not items:
            conditions = json.dumps({"page_size": 100, "page": 1}, separators=(",", ":"))
            try:
                data = self.http.get_json(
                    f"/api/course/{course_id}/coursewares", {"conditions": conditions}
                )
                items = _first_list(data, ("coursewares", "courseware", "activities"))
            except (ApiError, TronClassError) as exc:
                log.debug("coursewares 回退请求失败：%s", exc)
        return items

    # ---- 附件与签名下载 ----------------------------------------------
    def attachments_of_activity(self, activity: "Activity") -> list[Attachment]:
        """活动响应里的 uploads 就是附件表，省掉一次接口往返；为空时才回退单独查询。"""
        uploads = activity.raw.get("uploads") if isinstance(activity.raw, dict) else None
        if isinstance(uploads, list) and uploads:
            result = []
            for upload in uploads:
                if not isinstance(upload, dict):
                    continue
                att = _attachment_from_upload(upload)
                if att:
                    att.activity_id = activity.id
                    result.append(att)
            if result:
                return result
        return self.upload_references(activity.id)

    def upload_references(self, activity_id: str) -> list[Attachment]:
        try:
            data = self.http.get_json(f"/api/activities/{activity_id}/upload_references")
        except ApiError as exc:
            if exc.status in (403, 404):
                return []
            raise
        items = _first_list(data, ("references", "attachments", "uploads", "files"))
        result = []
        for item in items:
            att = _attachment_from_upload(item)
            if not att:
                continue
            att.activity_id = str(activity_id)
            result.append(att)
        return result

    def resolve_download_url(self, att: Attachment) -> str:
        """把文件引用换成带签名的直链。依次尝试几种已知路径。"""
        hint = att.url_hint
        if hint.startswith("http"):
            return hint

        candidates = []
        if att.ref_id:
            candidates.append(f"/api/uploads/reference/document/{att.ref_id}/url")
        if att.upload_id:
            candidates.append(f"/api/uploads/{att.upload_id}/url")
        if att.ref_id and att.upload_id != att.ref_id:
            candidates.append(f"/api/uploads/{att.ref_id}/url")

        last: Exception | None = None
        for path in candidates:
            try:
                data = self.http.get_json(path)
            except ApiError as exc:
                last = exc
                if exc.status in (400, 404, 405, 422):
                    continue
                raise
            url = _pick(data, "url", "download_url", "signed_url", "link", default="")
            if not url and isinstance(data, dict):
                # 少数版本把地址放在 data / result 里
                inner = data.get("data") or data.get("result")
                if isinstance(inner, dict):
                    url = _pick(inner, "url", "download_url")
            if url:
                return str(url)
        raise TronClassError(f"无法获取下载链接：{att.name}（{last}）")

    # ---- 公告 --------------------------------------------------------
    def platform_announcements(self, page: int = 1, page_size: int = 50) -> tuple[list[Announcement], int]:
        data = self.http.get_json(
            "/api/announcement", {"page": page, "page_size": page_size}
        )
        items = _first_list(data, ("announcements", "announcement"))
        total = _as_int(_pick(data, "total", "count", "total_count", default=0)) if isinstance(data, dict) else 0
        result = [self._to_announcement(item, scope="platform") for item in items]
        return [a for a in result if a], total

    def course_announcements(self, course: Course, limit: int = 50) -> list[Announcement]:
        """课程公告。深大的端点是 /api/courses/{id}/bulletins（复数 courses）。"""
        for path in (
            f"/api/courses/{course.id}/bulletins",
            f"/api/course/{course.id}/bulletins",
            f"/api/courses/{course.id}/announcements",
        ):
            try:
                data = self.http.get_json(
                    path, {"page": 1, "page_size": limit, "no-intercept": "true"}
                )
            except ApiError as exc:
                if exc.status in (403, 404, 405):
                    continue
                raise
            items = _first_list(data, ("bulletins", "announcements", "announcement"))
            if not items:
                continue
            result = []
            for item in items:
                ann = self._to_announcement(item, scope="course")
                if ann:
                    ann.course = course.name
                    ann.course_id = course.id
                    result.append(ann)
            return result
        return []

    def announcement_detail(self, ann: Announcement) -> str:
        """拉取公告正文（纯文本以外的 HTML 会原样返回，由上层清洗）。"""
        if ann.content:
            return ann.content
        for path in (
            f"/api/announcements/{ann.id}",
            f"/api/announcement/{ann.id}",
        ):
            try:
                data = self.http.get_json(path)
            except ApiError as exc:
                if exc.status in (403, 404):
                    continue
                raise
            if not isinstance(data, dict):
                continue
            body = _pick(data, "content", "body", "description", "text", default="")
            if body:
                return str(body)
        return ""

    @staticmethod
    def _to_announcement(item: dict[str, Any], scope: str) -> Announcement | None:
        ann_id = _pick(item, "id", "announcement_id")
        title = _pick(item, "title", "name", "subject")
        if ann_id is None or not title:
            return None
        author = _pick(item, "publisher", "author", "created_by", "user_name", default="")
        if isinstance(author, dict):
            author = _pick(author, "name", "display_name", "username", default="")
        course_name = _pick(item, "course_name", default="")
        if isinstance(course_name, dict):
            course_name = _pick(course_name, "name", default="")
        return Announcement(
            id=str(ann_id),
            title=str(title),
            course=str(course_name or ""),
            course_id=str(_pick(item, "course_id", default="")),
            author=str(author or ""),
            published_at=to_datetime(
                _pick(item, "published_at", "publish_time", "created_at", "start_time")
            ),
            content=str(_pick(item, "content", "body", "description", default="")),
            scope=scope,
            attachment_count=len(_first_list(item, ("attachments", "upload_references")))
            or _as_int(_pick(item, "attachment_count", default=0)),
        )


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    """兼容 true / "true" / 1 / "1"，缺省视为 True。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _attachment_from_upload(upload: dict[str, Any]) -> Attachment | None:
    """把 uploads 里的一项转成 Attachment。

    allow_download 是平台明确给出的授权标记，为 false 时上层必须跳过，
    这是本工具的合规底线，不做任何绕过。
    """
    name = _pick(upload, "name", "filename", "file_name", "title")
    if not name:
        return None
    reference_id = _pick(upload, "reference_id", "referenceId")
    upload_id = _pick(upload, "id", "upload_id")
    if reference_id is None and upload_id is None:
        return None

    allowed = _truthy(upload.get("allow_download")) and _truthy(
        upload.get("origin_allow_download")
    )
    return Attachment(
        ref_id=str(reference_id if reference_id is not None else upload_id),
        name=str(name),
        size=_as_int(_pick(upload, "size", "file_size", "bytes", default=0)),
        upload_id=str(upload_id or ""),
        url_hint=str(_pick(upload, "url", "download_url", "link", default="") or ""),
        kind=str(_pick(upload, "type", "mime", default="") or "").lower(),
        allow_download=allowed,
        raw=upload,
    )
