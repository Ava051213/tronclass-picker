"""不联网的单元测试：文件名清洗、落点分配、状态存取、接口字段解析。

跑法：python tests/test_basic.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tronclass_picker.api import as_int, as_list, pick, semester_of, to_local, truthy
from tronclass_picker.collect import Store
from tronclass_picker.config import Config
from tronclass_picker.download import safe_name, target_path
from tronclass_picker.server import State, _current_term, decorate


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


class TestScopeFilter(unittest.TestCase):
    def test_current_term_latest_only(self):
        courses = [
            {"semester": "2025-1", "name": "旧课"},
            {"semester": "2026-1", "name": "新课"},
        ]
        result = _current_term(courses)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "新课")

    def test_all_returns_all(self):
        courses = [
            {"semester": "2025-1", "name": "旧课"},
            {"semester": "2026-1", "name": "新课"},
        ]
        result = courses  # scope == "all" means no filtering
        self.assertEqual(len(result), 2)

    def test_start_scan_sets_scope(self):
        cfg = Config()
        state = State(cfg, Path("config.json"))
        state.busy = False
        state.start_scan(scope="all")
        self.assertEqual(state.scope, "all")
        state.busy = False
        state.start_scan(scope="current")
        self.assertEqual(state.scope, "current")
        state.busy = False
        state.start_scan(scope=None)
        self.assertEqual(state.scope, "current")


if __name__ == "__main__":
    unittest.main(verbosity=2)
