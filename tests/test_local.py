"""离线自测：用本地 mock 服务模拟畅课接口，验证下载/去重/通知/失效检测。

运行：
    python tests/test_local.py
不依赖网络，也不需要真实账号。
"""

from __future__ import annotations

import builtins
from datetime import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tronclass_picker import cli, config, interactive  # noqa: E402
from tronclass_picker.api import Course  # noqa: E402
from tronclass_picker.models import ScanResult  # noqa: E402


class FakeApp:
    """只实现 scan 的最小替身，供向导测试使用。"""

    def __init__(self, tasks=None) -> None:
        self._tasks = tasks or []

    def scan(self, courses, extensions=None) -> ScanResult:
        return ScanResult(tasks=list(self._tasks))


COURSE_ID = "1001"
FILES = {
    "讲义1.pdf": b"%PDF-1.4 fake thesis\r\n" * 40,
    "实验指导.docx": b"PK\x03\x04 fake docx content" * 30,
    "大纲.txt": b"syllabus\n" * 10,
    "教师私有.pdf": b"%PDF-1.4 restricted\r\n" * 20,
}

COURSES = {
    "courses": [
        # 结构与真实租户一致：学期在 academic_year.name + semester.code 里
        {"id": COURSE_ID, "name": "数字集成电路与系统设计", "course_code": "CSE301",
         "academic_year": {"code": "2026", "name": "2026-2027"},
         "semester": {"code": "2026-1", "name": None},
         "department": {"name": "计算机与软件学院"}},
        {"id": "1002", "name": "计算机图形学", "course_code": "CSE302",
         "academic_year": {"code": "2025", "name": "2025-2026"},
         "semester": {"code": "2025-2", "name": None},
         "department": {"name": "计算机与软件学院"}},
    ]
}

ACTIVITIES = {
    "activities": [
        # 真实租户里 attachments 内嵌在活动上，不用再单独查一次
        {"id": "5001", "title": "第3章 时序逻辑", "type": "material",
         "start_time": "2026-10-20T08:00:00Z",
         "uploads": [
             {"id": 1, "reference_id": 9001, "name": "讲义1.pdf",
              "size": len(FILES["讲义1.pdf"]), "type": "document", "allow_download": True},
             {"id": 2, "reference_id": 9002, "name": "大纲.txt",
              "size": len(FILES["大纲.txt"]), "type": "document", "allow_download": True},
             {"id": 3, "reference_id": 9009, "name": "封面.png",
              "size": 100, "type": "document", "allow_download": True},
             {"id": 4, "reference_id": 9010, "name": "破解工具.exe",
              "size": 100, "type": "document", "allow_download": True},
             {"id": 5, "reference_id": 9011, "name": "教师私有.pdf",
              "size": 100, "type": "document", "allow_download": False},
         ]},
        # 这个活动不带 uploads，用来验证回退到 upload_references
        {"id": "5002", "title": "第4章 组合逻辑", "type": "document",
         "start_time": "2026-10-22T08:00:00Z"},
        {"id": "5003", "title": "第一次作业", "type": "homework",
         "start_time": "2026-10-24T08:00:00Z"},
    ]
}

REFS = {
    "5001": {"references": [
        {"id": "9001", "name": "讲义1.pdf", "size": len(FILES["讲义1.pdf"]), "upload_id": "u1"},
        {"id": "9002", "name": "大纲.txt", "size": len(FILES["大纲.txt"])},
        {"id": "9009", "name": "封面.png", "size": 100},
        {"id": "9010", "name": "破解工具.exe", "size": 100},
    ]},
    "5002": {"references": [
        {"id": "9003", "name": "实验指导.docx", "size": len(FILES["实验指导.docx"])},
    ]},
    "5003": {"references": [
        {"id": "9004", "name": "答案.pdf", "size": 10},
    ]},
}

COURSEWARES = {
    "coursewares": [
        {"id": "7001", "name": "课件-第1讲",
         "uploads": [{"id": "9101", "name": "第1讲_绪论.pdf", "size": 2048}]},
    ]
}

# 引用 id → 文件名，供 mock 的签名下载接口查表
REF_NAME = {ref["id"]: ref["name"] for group in REFS.values() for ref in group["references"]}
REF_NAME["9101"] = "第1讲_绪论.pdf"
REF_NAME["9011"] = "教师私有.pdf"

PLATFORM_ANN = {
    "announcements": [
        {"id": "a1", "title": "关于期中教学检查的通知", "publisher": "教务处",
         "published_at": "2026-10-27 09:30:00", "content": "<p>请各位同学<b>按时</b>完成评教。</p>"},
        {"id": "a2", "title": "校园网维护公告", "publisher": "信息中心",
         "published_at": "2026-01-02 10:00:00", "content": "旧公告"},
    ],
    "total": 2,
}

COURSE_ANN = {
    "bulletins": [
        {"id": "b1", "title": "本周实验课调整到周四",
         "created_by": {"id": 1, "name": "张老师"},
         "created_at": "2026-10-28T14:00:00Z",
         "content": "<div>因故调整，请互相转告。</div>", "uploads": [{"id": 1}]},
    ]
}

# 另一门课（上一学期）的公告，用来验证通知采集不受文档筛选影响
COURSE_ANN_1002 = {
    "bulletins": [
        {"id": "b2", "title": "上学期结课材料归档说明",
         "created_by": {"id": 2, "name": "李老师"},
         "created_at": "2026-09-15T10:00:00Z",
         "content": "<p>课件已全部上传。</p>", "uploads": []},
    ]
}


class Handler(BaseHTTPRequestHandler):
    fail_auth = False

    def log_message(self, *args):  # 静音
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, body: bytes, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if self.fail_auth or not self.headers.get("Cookie", "").startswith("sessionid=good"):
            # 未登录时统一认证会把请求打回 HTML 登录页
            self._raw(
                b"<!DOCTYPE html><html><head><title>\xe7\x99\xbb\xe5\xbd\x95</title></head>"
                b"<body>login please</body></html>",
                "text/html; charset=utf-8",
            )
            return

        if path == "/api/user/profile":
            self._json({"name": "炸炸", "username": "20230001"})
        elif path == "/api/my-courses":
            self._json(COURSES)
        elif path.startswith("/api/courses/") and path.endswith("/activities"):
            self._json(ACTIVITIES if COURSE_ID in path else {"activities": []})
        elif path.startswith("/api/course/") and path.endswith("/coursewares"):
            self._json(COURSEWARES if COURSE_ID in path else {"coursewares": []})
        elif path.startswith("/api/activities/") and path.endswith("/upload_references"):
            aid = path.split("/")[3]
            self._json(REFS.get(aid, {"references": []}))
        elif path.startswith("/api/uploads/reference/document/"):
            ref = path.removeprefix("/api/uploads/reference/document/").removesuffix("/url")
            name = REF_NAME.get(ref)
            if not name:
                self._json({"error": "not found"}, 404)
                return
            self._json({"url": f"http://127.0.0.1:{self.server.server_port}/dl/"
                               + urllib.parse.quote(name)})
        elif path == "/api/announcement":
            self._json(PLATFORM_ANN)
        elif path.startswith("/api/courses/") and path.endswith("/bulletins"):
            self._json(COURSE_ANN if COURSE_ID in path else COURSE_ANN_1002)
        elif path.startswith("/api/courses/") and path.endswith("/announcements"):
            # 真实租户里这条是老路径，已废弃
            self._json({"error": "not found"}, 404)
        elif path.startswith("/dl/"):
            name = urllib.parse.unquote(path[4:])
            if name == "第1讲_绪论.pdf":
                self._raw(b"%PDF-1.7 courseware" * 20)
            elif name in FILES:
                self._raw(FILES[name])
            else:
                self._json({"error": "missing"}, 404)
        else:
            self._json({"error": "unmapped", "path": path}, 404)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def write_config(
    path: Path, port: int, cookie: str, *, output: str = "output", semester: str = ""
) -> None:
    data = {
        "base_url": f"http://127.0.0.1:{port}",
        "auth": {"cookie": cookie},
        "paths": {"output": str(path.parent / output)},
        "network": {"request_interval": 0, "retries": 1, "timeout": 10},
        "notices": {"days": 90},
    }
    if semester:
        data["courses"] = {"semester": semester}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    server = start_server()
    port = server.server_port
    workdir = Path(tempfile.mkdtemp(prefix="tcp-test-"))
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ← {detail}" if detail and not ok else ""))

    try:
        cfg_path = workdir / "config.json"
        write_config(cfg_path, port, "sessionid=good; csrftoken=abc")
        out = workdir / "output"

        print("\n[1] check 子命令")
        code = cli.main(["--config", str(cfg_path), "--no-banner", "check"])
        check("会话校验通过", code == 0, f"退出码 {code}")

        print("\n[2] 首次运行：下载 + 汇总")
        code = cli.main(["--config", str(cfg_path), "--no-banner"])
        check("退出码为 0", code == 0, f"退出码 {code}")

        course_dir = out / "documents" / "数字集成电路与系统设计"
        for name, content in FILES.items():
            if name == "教师私有.pdf":
                continue   # 受限文件默认不下，单独在 [9] 验证
            target = course_dir / name
            check(f"下载 {name}", target.is_file() and target.read_bytes() == content)
        check("内嵌 uploads 的文件已下载", (course_dir / "讲义1.pdf").is_file())
        check("回退 upload_references 的文件已下载", (course_dir / "实验指导.docx").is_file())
        check("coursewares 默认不重复采集",
              not (course_dir / "第1讲_绪论.pdf").exists())
        check("非文档扩展名被过滤",
              not (out / "documents" / "数字集成电路与系统设计" / "封面.png").exists())
        check("可执行文件被过滤",
              not (out / "documents" / "数字集成电路与系统设计" / "破解工具.exe").exists())
        check("平台禁止下载的已跳过",
              not (out / "documents" / "数字集成电路与系统设计" / "教师私有.pdf").exists())
        check("homework 活动不采集附件",
              not (out / "documents" / "数字集成电路与系统设计" / "答案.pdf").exists())

        notice_files = list((out / "notices").glob("*_通知汇总.md"))
        check("生成通知汇总文件", len(notice_files) == 1, f"找到 {len(notice_files)} 个")
        if notice_files:
            text = notice_files[0].read_text(encoding="utf-8")
            check("汇总含平台公告", "关于期中教学检查的通知" in text)
            check("汇总含课程通知", "本周实验课调整到周四" in text)
            check("HTML 已转纯文本", "<p>" not in text and "按时" in text)
            check("超期公告被过滤", "校园网维护公告" not in text)
            check("按课程分组", "### 数字集成电路与系统设计" in text)
        check("latest.md 已生成", (out / "notices" / "latest.md").is_file())
        check("日志文件已生成", any((out / "logs").glob("run-*.log")))

        print("\n[3] 二次运行：增量去重")
        before = {p: p.stat().st_mtime for p in course_dir.glob("*")}
        code = cli.main(["--config", str(cfg_path), "--no-banner"])
        after = {p: p.stat().st_mtime for p in course_dir.glob("*")}
        check("二次运行未重复下载", before == after)
        check("二次未生成新通知文件",
              len(list((out / "notices").glob("*_通知汇总.md"))) == 1)

        print("\n[4] 会话失效检测")
        write_config(cfg_path, port, "sessionid=expired")
        code = cli.main(["--config", str(cfg_path), "--no-banner", "-q"])
        check("失效时退出码为 2", code == 2, f"退出码 {code}")

        print("\n[5] dry-run")
        write_config(cfg_path, port, "sessionid=good")
        shutil.rmtree(course_dir, ignore_errors=True)
        code = cli.main(["--config", str(cfg_path), "--no-banner", "--dry-run", "--only", "docs"])
        check("演练模式退出码 0", code == 0, f"退出码 {code}")
        check("演练不落盘", not course_dir.exists())

        print("\n[6] 下载按学期收窄，通知保持全量")
        write_config(cfg_path, port, "sessionid=good", output="output6", semester="2026-2027")
        out6 = workdir / "output6"
        code = cli.main(["--config", str(cfg_path), "--no-banner"])
        check("退出码 0", code == 0, f"退出码 {code}")
        check("只下载本学期课程",
              (out6 / "documents" / "数字集成电路与系统设计" / "讲义1.pdf").is_file())
        check("上一学期课程未下载", not (out6 / "documents" / "计算机图形学").exists())

        notice6 = list((out6 / "notices").glob("*_通知汇总.md"))
        text6 = notice6[0].read_text(encoding="utf-8") if notice6 else ""
        check("通知仍采集全部课程", "上学期结课材料归档说明" in text6)
        check("汇总里两门课都在",
              "### 计算机图形学" in text6 and "### 数字集成电路与系统设计" in text6)

        logs6 = sorted((out6 / "logs").glob("run-*.log"))
        log6 = logs6[0].read_text(encoding="utf-8", errors="replace") if logs6 else ""
        check("通知标题写进了日志", "上学期结课材料归档说明" in log6)
        check("通知正文摘要写进了日志", "课件已全部上传" in log6)

        print("\n[7] 交互向导")
        check("逗号选择", interactive.parse_picks("1,3", 5) == [0, 2])
        check("区间选择", interactive.parse_picks("2-4", 5) == [1, 2, 3])
        check("全选", interactive.parse_picks("a", 3) == [0, 1, 2])
        check("越界被丢弃", interactive.parse_picks("1,9,-2", 3) == [0])
        check("中文逗号", interactive.parse_picks("1，2", 3) == [0, 1])

        fake = [
            Course(id="1", name="甲课", semester="2026-2027 第1学期"),
            Course(id="2", name="乙课", semester="2026-2027 第1学期"),
            Course(id="3", name="丙课", semester="2025-2026 第2学期"),
        ]
        check("本学期按时间取最近", interactive._current_semester(fake) == "2026-2027 第1学期")

        # 上学期组课程更多时，不能因为数量多就把上学期当成本学期
        mixed = [
            Course(id="1", name="新课", semester="2026-2027 第1学期"),
            Course(id="2", name="旧课A", semester="2025-2026 第2学期"),
            Course(id="3", name="旧课B", semester="2025-2026 第2学期"),
        ]
        check("学期不受课程数量影响",
              interactive._current_semester(mixed) == "2026-2027 第1学期")

        dated = [
            Course(id="1", name="新课", semester="2026-2027 第1学期",
                   start_date=datetime(2026, 8, 31)),
            Course(id="2", name="旧课", semester="2025-2026 第2学期",
                   start_date=datetime(2026, 2, 20)),
        ]
        check("有开课日期时按日期判断",
              interactive._current_semester(dated) == "2026-2027 第1学期")

        future = [
            Course(id="1", name="当前课", semester="2026-2027 第1学期",
                   start_date=datetime(2026, 8, 31)),
            Course(id="2", name="下学期课", semester="2026-2027 第2学期",
                   start_date=datetime(2099, 2, 22)),
        ]
        check("还没开课的学期不算本学期",
              interactive._current_semester(future) == "2026-2027 第1学期")

        from pathlib import Path as _Path
        from tronclass_picker.api import Attachment
        from tronclass_picker.models import DocTask

        fake_tasks = [
            DocTask(key=f"k{i}", course=c, source="测试",
                    attachment=Attachment(ref_id=str(i), name=f"讲义{i}.pdf", size=1048576),
                    target=_Path(f"x{i}.pdf"))
            for i, c in enumerate(fake, 1)
        ]
        original_input = builtins.input

        # 主菜单「两样都做」/ 本学期 / 空回车全选 / 确认
        answers = iter(["3", "1", "", "y"])
        builtins.input = lambda *a: next(answers)
        try:
            sel = interactive.ask(FakeApp(fake_tasks), config.load(None), fake, "测试")
        finally:
            builtins.input = original_input
        check("菜单命中本学期 2 门", len(sel.courses) == 2)
        check("空回车等于全选文件", len(sel.tasks) == 3)
        check("两样都做时两个开关都开", sel.want_documents and sel.want_notices)

        # 主菜单「下载课程文件」+ 按编号挑选
        answers = iter(["1", "1", "1-2", "y"])
        builtins.input = lambda *a: next(answers)
        try:
            sel2 = interactive.ask(FakeApp(fake_tasks), config.load(None), fake, "测试")
        finally:
            builtins.input = original_input
        check("只下载时通知关闭", sel2.want_documents and not sel2.want_notices)
        check("文件挑选按编号生效",
              [t.attachment.name for t in sel2.tasks] == ["讲义1.pdf", "讲义2.pdf"])

        # 扫描不到文件时直接取消，不留半截任务
        answers = iter(["1", "1"])
        builtins.input = lambda *a: next(answers)
        try:
            interactive.ask(FakeApp([]), config.load(None), fake, "测试")
            cancelled = False
        except interactive.Cancelled:
            cancelled = True
        finally:
            builtins.input = original_input
        check("扫描无文件时取消", cancelled)

        # 主菜单只汇总通知，不选课程、不扫描文件
        answers = iter(["2", "y"])
        builtins.input = lambda *a: next(answers)
        try:
            sel4 = interactive.ask(FakeApp(fake_tasks), config.load(None), fake, "测试")
        finally:
            builtins.input = original_input
        check("只汇总通知时不扫文件", sel4.want_notices and not sel4.want_documents)
        check("只汇总通知时覆盖全部课程", len(sel4.courses) == 3)


        original_tty = interactive.is_interactive
        interactive.is_interactive = lambda: False
        try:
            code = cli.main(["--config", str(cfg_path), "--no-banner", "pick"])
        finally:
            interactive.is_interactive = original_tty
        check("非终端下拒绝 pick", code == 3, f"退出码 {code}")

        print("\n[8] 决策层纯逻辑（不联网）")
        from tronclass_picker import planning
        from tronclass_picker.models import Selection

        check("文件名清洗非法字符", planning.safe_filename("a/b:c*.pdf") == "a_b_c_.pdf")
        check("中文文件名保持不变", planning.safe_filename("第1章 绪论.pdf") == "第1章 绪论.pdf")
        check("Windows 保留名加前缀", planning.safe_filename("CON.txt") == "_CON.txt")
        check("超长名截到 100 字符", len(planning.safe_filename("x" * 200 + ".pdf")) == 100)
        check("大小写扩展名归一", planning.guess_extension("a.PDF") == ".pdf")
        check("无扩展名兜底", planning.guess_extension("没有后缀") == ".bin")

        tcfg = config.load(None)
        used: set[str] = set()
        first = planning.resolve_target(tcfg, fake[0], "同名.pdf", used)
        second = planning.resolve_target(tcfg, fake[0], "同名.pdf", used)
        check("同名文件自动加序号",
              first != second and second.stem.endswith("_2"))

        tcfg.courses.semester = "2026-2027"
        check("按学期筛选", len(planning.select_courses(fake, tcfg)) == 2)
        tcfg.courses.exclude = ["甲课"]
        check("exclude 生效", len(planning.select_courses(fake, tcfg)) == 1)
        check("scope=False 忽略学期筛选",
              len(planning.select_courses(fake, tcfg, scope=False)) == 2)
        tcfg.courses.semester = ""
        tcfg.courses.exclude = []

        planner = planning.Planner(tcfg, FakeApp(fake_tasks))
        job = planner.build(
            Selection(courses=fake[:2], want_documents=True, want_notices=True),
            fake,
        )
        check("规划器复用已挑好的文件", len(job.doc_tasks) == 3)
        check("通知跟随所选课程", len(job.notice_courses) == 2)

        job2 = planner.build(
            Selection(courses=fake[:2], want_documents=False, want_notices=True,
                      notices_follow=False),
            fake,
        )
        check("不做文档时不产生任务", job2.doc_tasks == [])
        check("通知可回退到全量", len(job2.notice_courses) == 3)

        job3 = planner.build(
            Selection(courses=fake[:2], want_documents=True, want_notices=False), fake
        )
        check("只做文档时不扫通知", job3.notice_courses == [])

        print("\n[9] 受限文件（平台标记不允许下载）")
        write_config(cfg_path, port, "sessionid=good", output="output9")
        code = cli.main(["--config", str(cfg_path), "--no-banner", "--only", "docs"])
        out9 = workdir / "output9"
        restricted_path = out9 / "documents" / "数字集成电路与系统设计" / "教师私有.pdf"
        check("默认不下载受限文件", code == 0 and not restricted_path.exists())

        write_config(cfg_path, port, "sessionid=good", output="output9b")
        code = cli.main([
            "--config", str(cfg_path), "--no-banner",
            "--only", "docs", "--include-restricted",
        ])
        out9b = workdir / "output9b"
        restricted_b = out9b / "documents" / "数字集成电路与系统设计" / "教师私有.pdf"
        check("开启后尝试并成功取到", code == 0 and restricted_b.is_file())
        if restricted_b.is_file():
            check("受限文件内容正确",
                  restricted_b.read_bytes() == FILES["教师私有.pdf"])

        print("\n" + "=" * 54)
        passed = sum(1 for _, ok, _ in checks if ok)
        print(f"  通过 {passed}/{len(checks)}")
        print("=" * 54)
        return 0 if passed == len(checks) else 1
    finally:
        server.shutdown()
        if os.environ.get("KEEP"):
            print(f"\n产物保留在：{workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
