"""不联网的单元测试：文件名清洗、落点分配、状态存取、接口字段解析。

跑法：python tests/test_basic.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tronclass_picker.api import as_int, as_list, pick, semester_of, to_local, truthy
from tronclass_picker.client import (
    ApiError,
    AuthExpired,
    HttpClient,
    NetworkError,
    TronClassError,
    parse_cookie_input,
)
from tronclass_picker.collect import Store
from tronclass_picker.config import Config
from tronclass_picker.download import safe_name, target_path
from tronclass_picker.server import Handler, State, _current_term, decorate, scope_courses


class TestSafeName(unittest.TestCase):
    def test_illegal_chars_replaced(self):
        self.assertEqual(safe_name('a/b:c*d?.pdf'), "a_b_c_d_.pdf")

    def test_strips_and_collapses_space(self):
        self.assertEqual(safe_name("  实验   一 .docx "), "实验 一.docx")

    def test_reserved_word_escaped(self):
        self.assertTrue(safe_name("con.pdf").startswith("_con"))

    def test_empty_falls_back(self):
        self.assertEqual(safe_name("   "), "未命名")

    def test_long_name_trimmed(self):
        name = safe_name("字" * 200 + ".pdf")
        self.assertLessEqual(len(name), 96)
        self.assertTrue(name.endswith(".pdf"))


class TestTargetPath(unittest.TestCase):
    def test_course_folder(self):
        used: set[str] = set()
        p = target_path(Path("out"), "数据库系统", "作业一.docx", used)
        self.assertEqual(p, Path("out") / "数据库系统" / "作业一.docx")

    def test_same_name_gets_suffix(self):
        used: set[str] = set()
        first = target_path(Path("out"), "课", "a.pdf", used)
        second = target_path(Path("out"), "课", "a.pdf", used)
        self.assertEqual(first.name, "a.pdf")
        self.assertEqual(second.name, "a_2.pdf")

    def test_same_name_different_case_gets_suffix(self):
        # NTFS 大小写不敏感，落点去重也得按这个语义来
        used: set[str] = set()
        target_path(Path("out"), "课", "Report.pdf", used)
        second = target_path(Path("out"), "课", "report.pdf", used)
        self.assertEqual(second.name, "report_2.pdf")


class TestStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".state.json"
            store = Store(path)
            store.mark_known(["1:2", "1:3"])
            store.mark_done("1:2", Path(tmp) / "a.pdf", 12)
            store.save()

            again = Store(path)
            self.assertEqual(again.known, {"1:2", "1:3"})
            self.assertTrue(again.is_done("1:2"))
            self.assertFalse(again.is_done("9:9"))

    def test_broken_file_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".state.json"
            path.write_text("{ 坏掉的 json", encoding="utf-8")
            store = Store(path)
            self.assertEqual(store.known, set())


class TestParsing(unittest.TestCase):
    def test_pick_skips_empty(self):
        self.assertEqual(pick({"a": "", "b": 0, "c": "x"}, "a", "b", "c"), 0)
        self.assertEqual(pick({"a": None, "b": "y"}, "a", "b"), "y")
        self.assertIsNone(pick({}, "a"))

    def test_as_list_unwraps(self):
        self.assertEqual(len(as_list({"courses": [{"id": 1}, {"id": 2}]}, "courses")), 2)
        self.assertEqual(len(as_list({"data": {"items": [{"a": 1}]}}, "x")), 1)
        self.assertEqual(len(as_list([{"a": 1}], "x")), 1)
        self.assertEqual(as_list("nope", "x"), [])

    def test_truthy(self):
        self.assertTrue(truthy(None))
        self.assertFalse(truthy("false"))
        self.assertFalse(truthy(0))
        self.assertTrue(truthy("1"))

    def test_as_int(self):
        self.assertEqual(as_int("1234"), 1234)
        self.assertEqual(as_int("12.7"), 12)
        self.assertEqual(as_int(None), 0)

    def test_to_local_plain(self):
        dt = to_local("2026-11-24 15:59:00")
        self.assertEqual((dt.year, dt.hour), (2026, 15))

    def test_to_local_timestamp(self):
        dt = to_local(1793667600000)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_semester_from_code(self):
        item = {"academic_year": {"name": "2026-2027"},
                "semester": {"code": "2026-1", "name": None, "real_name": None}}
        self.assertEqual(semester_of(item), "2026-2027 第1学期")

    def test_semester_direct_name(self):
        self.assertEqual(semester_of({"semester": "秋季学期"}), "秋季学期")


class TestTodoPayload(unittest.TestCase):
    """外部接口的返回格式别悄悄变：小程序按这些键取值。"""

    def test_hours_left_and_overdue(self):
        now = datetime(2026, 11, 3, 12, 0, 0)
        rows = decorate(
            [
                {"title": "明天交", "due_ts": "2026-11-04T12:00:00"},
                {"title": "已过", "due_ts": "2026-11-03T10:00:00"},
                {"title": "没写截止", "due_ts": ""},
            ],
            now=now,
        )
        self.assertEqual(rows[0]["hours_left"], 24.0)
        self.assertFalse(rows[0]["overdue"])
        self.assertTrue(rows[1]["overdue"])
        self.assertIsNone(rows[2]["hours_left"])
        self.assertFalse(rows[2]["overdue"])

    def test_bad_due_ts_does_not_crash(self):
        rows = decorate([{"title": "x", "due_ts": "不是时间"}])
        self.assertIsNone(rows[0]["hours_left"])

    def test_default_port_is_fixed(self):
        self.assertEqual(Config().server.port, 8765)


class TestStaticPathSafety(unittest.TestCase):
    def test_traversal_blocked(self):
        root = (Path(__file__).resolve().parent.parent / "tronclass_picker" / "static").resolve()
        target = (root / "../../config.json").resolve()
        self.assertFalse(target.is_relative_to(root))

    def test_normal_file_allowed(self):
        root = (Path(__file__).resolve().parent.parent / "tronclass_picker" / "static").resolve()
        target = (root / "app.js").resolve()
        self.assertTrue(target.is_relative_to(root))


class TestLogEntry(unittest.TestCase):
    def test_add_log_fields(self):
        cfg = Config()
        state = State(cfg, Path("config.json"))
        state.add_log("INFO", "测试消息")
        logs = state.get_logs()
        self.assertEqual(len(logs), 1)
        entry = logs[0]
        self.assertIn("ts", entry)
        self.assertIn("level", entry)
        self.assertIn("message", entry)
        self.assertIn("text", entry)
        self.assertIsInstance(entry["level"], int)
        self.assertEqual(entry["message"], "测试消息")
        self.assertTrue(entry["text"].endswith("测试消息"))


class TestSnapshotCache(unittest.TestCase):
    """空闲轮询靠 rev 缓存省序列化，失效机制锁死。"""

    def test_unchanged_state_reuses_bytes(self):
        state = State(Config(), Path("config.json"))
        first = state.snapshot_bytes()
        self.assertIs(first, state.snapshot_bytes())

    def test_set_invalidates_cache(self):
        state = State(Config(), Path("config.json"))
        first = state.snapshot_bytes()
        state.set(status="扫描完成", phase="done")
        second = state.snapshot_bytes()
        self.assertIsNot(first, second)
        self.assertIn("扫描完成".encode("utf-8"), second)

    def test_add_log_invalidates_cache(self):
        state = State(Config(), Path("config.json"))
        first = state.snapshot_bytes()
        state.add_log("INFO", "新日志")
        self.assertIsNot(first, state.snapshot_bytes())

    def test_clear_logs_invalidates_cache(self):
        state = State(Config(), Path("config.json"))
        state.add_log("INFO", "日志")
        state.snapshot_bytes()
        state.clear_logs()
        second = state.snapshot_bytes()
        self.assertIn(b'"logs": []', second)


class TestScopeFilter(unittest.TestCase):
    def test_current_term_latest_only(self):
        courses = [
            {"semester": "2025-1", "name": "旧课"},
            {"semester": "2026-1", "name": "新课"},
        ]
        result = _current_term(courses)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "新课")

    def test_scope_courses_all_returns_all(self):
        courses = [
            {"semester": "2025-1", "name": "旧课"},
            {"semester": "2026-1", "name": "新课"},
        ]
        result = scope_courses(courses, "all")
        self.assertEqual(len(result), 2)

    def test_scope_courses_current_returns_latest(self):
        courses = [
            {"semester": "2025-1", "name": "旧课"},
            {"semester": "2026-1", "name": "新课"},
        ]
        result = scope_courses(courses, "current")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "新课")

    def test_scope_courses_empty(self):
        self.assertEqual(scope_courses([], "current"), [])
        self.assertEqual(scope_courses([], "all"), [])

    def test_current_term_mixed_semester_formats(self):
        # real_name 和 code 拼的格式混着来时，直接比字符串会把第1学期排到第2学期前面
        courses = [
            {"semester": "2025-2026 第2学期", "name": "学期二"},
            {"semester": "2025-2026学年 第1学期", "name": "学年一"},
        ]
        result = _current_term(courses)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "学期二")

    def test_current_term_without_year_falls_back(self):
        courses = [
            {"semester": "春季学期", "name": "春"},
            {"semester": "秋季学期", "name": "秋"},
        ]
        self.assertEqual(_current_term(courses)[0]["name"], "秋")

    def test_start_scan_sets_scope(self):
        cfg = Config()
        state = State(cfg, Path("config.json"))
        state.busy = False
        with patch.object(State, "_scan"):
            state.start_scan(scope="all")
        self.assertEqual(state.scope, "all")
        state.busy = False
        with patch.object(State, "_scan"):
            state.start_scan(scope="current")
        self.assertEqual(state.scope, "current")
        state.busy = False
        with patch.object(State, "_scan"):
            state.start_scan(scope=None)
        self.assertEqual(state.scope, "current")


class TestLoginDetection(unittest.TestCase):
    """掉登录检测是全项目的安全网，特征矩阵锁死。"""

    def test_auth_url_is_login(self):
        body = "<html>统一身份认证</html>".encode("utf-8")
        self.assertTrue(HttpClient._looks_like_login(
            "https://authserver.szu.edu.cn/authserver/login", body, "text/html"))

    def test_lms_url_with_hint_is_not_enough(self):
        # URL 带 lms. 时不算登录页，落到内容判断；JSON 直接放行
        body = json.dumps({"user": "x"}).encode("utf-8")
        self.assertFalse(HttpClient._looks_like_login(
            "https://lms.szu.edu.cn/login-redirect?next=/a", body, "application/json"))

    def test_json_body_never_login(self):
        self.assertFalse(HttpClient._looks_like_login(
            "https://lms.szu.edu.cn/api/x", b"{}", "application/json"))

    def test_html_with_login_words(self):
        body = "<html><body>请登录 统一身份认证平台</body></html>".encode("utf-8")
        self.assertTrue(HttpClient._looks_like_login(
            "https://portal.example.edu/some/path", body, "text/html"))

    def test_html_without_login_words(self):
        body = b"<html><body>welcome to course page</body></html>"
        self.assertFalse(HttpClient._looks_like_login(
            "https://portal.example.edu/some/path", body, "text/html"))


class TestCookieParsing(unittest.TestCase):
    def test_plain_pairs(self):
        self.assertEqual(parse_cookie_input("a=1; b=2"), "a=1; b=2")

    def test_header_prefix_stripped(self):
        self.assertEqual(parse_cookie_input("Cookie: a=1"), "a=1")

    def test_multiline_joined(self):
        self.assertEqual(parse_cookie_input("a=1\nb=2"), "a=1; b=2")

    def test_items_without_equals_dropped(self):
        self.assertEqual(parse_cookie_input("random text; a=1"), "a=1")

    def test_empty(self):
        self.assertEqual(parse_cookie_input("   "), "")


class TestRetryPolicy(unittest.TestCase):
    """会话失效必须立刻抛；4xx 不重试；5xx/网络错按配置重试。"""

    def _client(self) -> HttpClient:
        cfg = Config()
        cfg.network.request_interval = 0
        return HttpClient(cfg)

    def test_auth_expired_raises_immediately(self):
        hc = self._client()
        calls: list[str] = []

        def fake_open(url, extra_headers=None):
            calls.append(url)
            raise AuthExpired("会话已失效")

        hc._open = fake_open
        with self.assertRaises(AuthExpired):
            hc.get_json("/api/x")
        self.assertEqual(len(calls), 1)

    def test_client_error_not_retried(self):
        hc = self._client()
        calls: list[str] = []

        def fake_open(url, extra_headers=None):
            calls.append(url)
            raise ApiError(404, url)

        hc._open = fake_open
        with self.assertRaises(ApiError):
            hc.get_json("/api/x")
        self.assertEqual(len(calls), 1)

    def test_server_error_retried(self):
        hc = self._client()
        hc.cfg.network.retries = 2
        calls: list[str] = []

        def fake_open(url, extra_headers=None):
            calls.append(url)
            raise NetworkError("超时")

        hc._open = fake_open
        with patch("tronclass_picker.client.time.sleep"):
            with self.assertRaises(NetworkError):
                hc.get_json("/api/x")
        self.assertEqual(len(calls), 2)


class _FakeState:
    """Handler 会碰到的最小面，行为按用例注入。"""

    def __init__(self):
        self.error: Exception | None = None
        self.todos: list[dict] = []
        self.downloads: list[list[str]] = []

    def live_todos(self) -> list[dict]:
        if self.error is not None:
            raise self.error
        return self.todos

    def snapshot(self) -> dict:
        return {"busy": False, "phase": "idle", "status": "", "files": [],
                "todos": [], "notices": [], "logs": [], "downloaded": [],
                "progress": {"done": 0, "total": 0}}

    def get_logs(self):
        return []

    def clear_logs(self):
        pass

    def start_scan(self, scope=None):
        pass

    def start_notices(self):
        pass

    def start_login(self):
        pass

    def start_download(self, keys):
        self.downloads.append(keys)


class TestTodoEndpoint(unittest.TestCase):
    """外部接口的错误契约（401/502）和 CORS 门，锁死。"""

    @classmethod
    def setUpClass(cls):
        cls.fake = _FakeState()
        Handler.state = cls.fake
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def tearDown(self):
        self.fake.error = None
        self.fake.todos = []
        self.fake.downloads.clear()

    def _get(self, path, origin=None):
        req = urllib.request.Request(self.base + path)
        if origin is not None:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8")), resp.headers
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, (json.loads(body) if body else {}), exc.headers

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_todos_ok_and_decorated(self):
        self.fake.todos = [{"title": "作业", "due_ts": "2999-01-01T00:00:00"}]
        code, payload, _ = self._get("/api/todos")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        row = payload["todos"][0]
        self.assertFalse(row["overdue"])
        self.assertIsNotNone(row["hours_left"])

    def test_auth_expired_is_401(self):
        self.fake.error = AuthExpired("会话已失效")
        code, payload, _ = self._get("/api/todos")
        self.assertEqual(code, 401)
        self.assertEqual(payload["error"], "auth_expired")

    def test_upstream_error_is_502(self):
        self.fake.error = TronClassError("平台异常")
        code, payload, _ = self._get("/api/todos")
        self.assertEqual(code, 502)
        self.assertEqual(payload["error"], "upstream")

    def test_health(self):
        code, payload, _ = self._get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_cors_open_for_local_and_absent(self):
        for origin in (None, "null", "http://127.0.0.1:8765", "http://localhost:3000"):
            _, _, headers = self._get("/api/health", origin=origin)
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*", origin)

    def test_cors_blocked_for_foreign_origin(self):
        _, _, headers = self._get("/api/health", origin="https://evil.example")
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_download_keys_must_be_list(self):
        code, _ = self._post("/api/download", {"keys": "a:1"})
        self.assertEqual(code, 400)

    def test_download_passes_keys(self):
        code, _ = self._post("/api/download", {"keys": ["a:1", "a:2"]})
        self.assertEqual(code, 200)
        self.assertEqual(self.fake.downloads, [["a:1", "a:2"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
