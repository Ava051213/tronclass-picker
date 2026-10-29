"""本地网页界面：起一个只监听 127.0.0.1 的小服务，浏览器里操作。

后端只提供 JSON 接口，前端是单个静态 HTML。不引入任何第三方依赖。
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import login
from .app import App
from .client import AuthExpired
from .config import Config, ConfigError, load
from .exits import EXIT_CONFIG, EXIT_OK
from .logx import setup as setup_logging
from .models import Course, DocTask, Selection
from .notices import html_to_text

log = logging.getLogger("web")

INDEX_PATH = Path(__file__).parent / "web" / "index.html"


class _BufferHandler(logging.Handler):
    """把日志留在内存里，供前端轮询取走。"""

    def __init__(self, sink: deque) -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("%(message)s"))
        self.setLevel(logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append({"level": record.levelno, "text": self.format(record)})
        except Exception:
            pass


class WebState:
    """所有可变状态集中在这里，读写都走锁。"""

    def __init__(self, cfg: Config, config_path: Path) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.lock = threading.Lock()
        self.app: App | None = None
        self.user = ""
        self.courses: list[Course] = []
        self.tasks: list[DocTask] = []
        self.busy = False
        self.status = "正在连接畅课…"
        self.progress = (0, 0)
        self.current = ""                      # 正在下载的文件名
        self.done_keys: set[str] = set()       # 本会话里已确认在库的任务键
        self.notice_items: list[dict] = []     # 最近一轮汇总的通知
        self.logs: deque = deque(maxlen=400)
        self.scope = "current"

        root = logging.getLogger()
        if root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)
        root.addHandler(_BufferHandler(self.logs))

        # 构造完自动扫一次，用户打开页面就有数据
        threading.Timer(0.4, self.reload).start()

    # ---- 快照 --------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "user": self.user,
                "courseCount": len(self.courses),
                "status": self.status,
                "busy": self.busy,
                "scope": self.scope,
                "progress": {"done": self.progress[0], "total": self.progress[1]},
                "current": self.current,
                "downloaded": sorted(self.done_keys),
                "notices": self.notice_items,
                "logs": list(self.logs)[-120:],
                "files": [
                    {
                        "key": t.key,
                        "course": t.course.name,
                        "name": t.attachment.name,
                        "size": t.size_hint,
                        "restricted": t.restricted,
                    }
                    for t in self.tasks
                ],
            }

    def set(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    # ---- 动作 --------------------------------------------------------
    def reload(self, scope: str | None = None) -> None:
        if self.busy:
            return
        if scope:
            self.set(scope=scope)
        self.set(busy=True, status="正在连接畅课…", progress=(0, 0))
        threading.Thread(target=self._reload_worker, daemon=True).start()

    def _reload_worker(self) -> None:
        try:
            if self.app is None:
                self.app = App(self.cfg)
            user, courses = self.app.connect()
        except AuthExpired:
            self.set(busy=False, status="登录已失效，点右上角重新登录")
            return
        except Exception as exc:
            self.set(busy=False, status=f"连接失败：{exc}")
            return

        self.set(user=user, courses=courses, status="正在扫描课程文件…")

        with self.lock:
            scope = self.scope
        try:
            picked = self._pick_courses(scope, courses)
            result = self.app.scan(picked)
        except AuthExpired:
            self.set(busy=False, status="登录已失效，点右上角重新登录")
            return
        except Exception as exc:
            self.set(busy=False, status=f"扫描失败：{exc}")
            return

        self.set(
            tasks=result.tasks,
            busy=False,
            status=f"扫描到 {len(result.tasks)} 个文件",
        )

    def _pick_courses(self, scope: str, courses: list[Course]) -> list[Course]:
        if scope == "all":
            return list(courses)
        current = _current_semester(courses)
        picked = [c for c in courses if c.semester == current]
        return picked or list(courses)

    def login(self) -> None:
        if self.busy:
            return
        self.set(busy=True, status="已打开浏览器，请在窗口里完成统一身份认证…")
        threading.Thread(target=self._login_worker, daemon=True).start()

    def _login_worker(self) -> None:
        try:
            code = login.run(self.cfg, self.config_path, wait=login.wait_for_login)
        except Exception as exc:
            self.set(busy=False, status=f"登录失败：{exc}")
            return
        if code != EXIT_OK:
            self.set(busy=False, status="登录没有完成，请重试")
            return
        self.app = None
        log.info("登录成功，重新加载课程…")
        self.reload()

    def download(self, keys: list[str]) -> None:
        if self.busy:
            return
        with self.lock:
            picked = [t for t in self.tasks if t.key in set(keys)]
            scope = self.scope
        if not picked:
            self.set(status="没有选中文件")
            return

        self.set(
            busy=True,
            status=f"开始下载 {len(picked)} 个文件…",
            progress=(0, len(picked)),
        )
        threading.Thread(target=self._download_worker, args=(picked, scope), daemon=True).start()

    def _download_worker(self, picked: list[DocTask], scope: str) -> None:
        assert self.app is not None
        try:
            selection = Selection(
                courses=self._pick_courses(scope, self.courses),
                want_documents=True,
                want_notices=False,
                tasks=picked,
                label="手动选择",
            )
            job = self.app.plan(selection)
            result = self.app.run(
                job,
                progress=lambda i, n, name: self.set(
                    progress=(i, n), current=name
                ),
            )
        except AuthExpired:
            self.set(busy=False, status="登录已失效，点右上角重新登录", current="")
            return
        except Exception as exc:
            self.set(busy=False, status=f"下载失败：{exc}", current="")
            return

        for key in result.success_keys:
            self.done_keys.add(key)
        tail = (
            f"下载完成：成功 {result.downloaded}，跳过 {result.skipped}，"
            f"失败 {len(result.failed)}，共 {result.bytes / 1048576:.1f} MB"
        )
        self.set(busy=False, status=tail, progress=(len(picked), len(picked)), current="")

    def collect_notices(self) -> None:
        if self.busy:
            return
        with self.lock:
            total = len(self.courses)
        self.set(busy=True, status=f"正在扫描 {total} 门课程的通知，约 1 分钟…", progress=(0, 1))
        threading.Thread(target=self._notice_worker, daemon=True).start()

    def _notice_worker(self) -> None:
        assert self.app is not None
        try:
            selection = Selection(
                courses=self.courses,
                want_documents=False,
                want_notices=True,
                notices_follow=False,
                label="全部课程",
            )
            job = self.app.plan(selection)
            result = self.app.run(job)
        except AuthExpired:
            self.set(busy=False, status="登录已失效，点右上角重新登录")
            return
        except Exception as exc:
            self.set(busy=False, status=f"汇总通知失败：{exc}")
            return

        items = result.notice_items or []
        if result.notice_file:
            tail = f"通知：新增 {result.notice_new} 条，已写入 {result.notice_file.name}"
        else:
            tail = "通知：这一轮没有新增"
        self.set(
            notice_items=[
                {
                    "course": a.course or ("平台公告" if a.scope == "platform" else ""),
                    "title": a.title,
                    "time": f"{a.published_at:%m-%d %H:%M}" if a.published_at else "",
                    "body": html_to_text(a.content)[:300],
                    "scope": a.scope,
                }
                for a in items[:300]
            ],
            busy=False,
            status=tail,
            progress=(1, 1),
        )


class Handler(BaseHTTPRequestHandler):
    state: WebState

    def log_message(self, *args):  # 静音默认访问日志
        pass

    # ---- 输出 --------------------------------------------------------
    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", code)

    # ---- 路由 --------------------------------------------------------
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = INDEX_PATH.read_text(encoding="utf-8")
            except OSError as exc:
                self._send(f"找不到页面：{exc}".encode(), "text/plain; charset=utf-8", 500)
                return
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            query = parse_qs(urlsplit(self.path).query)
            self._json(self.state.snapshot())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return

        if path == "/api/reload":
            self.state.reload(payload.get("scope"))
            self._json({"ok": True})
        elif path == "/api/login":
            self.state.login()
            self._json({"ok": True})
        elif path == "/api/download":
            keys = payload.get("keys") or []
            if not isinstance(keys, list):
                self._json({"error": "keys 必须是数组"}, 400)
                return
            self.state.download([str(k) for k in keys])
            self._json({"ok": True})
        elif path == "/api/notices":
            self.state.collect_notices()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def _current_semester(courses: list[Course]) -> str:
    from datetime import datetime

    dated = [c for c in courses if c.start_date and c.semester]
    if dated:
        today = datetime.now()
        started = [c for c in dated if c.start_date <= today]
        pool = started or dated
        picker = max if started else min
        return picker(pool, key=lambda c: c.start_date).semester
    named = [c.semester for c in courses if c.semester]
    return max(named) if named else ""


def main(config_path: str | Path | None = None) -> int:
    try:
        cfg = load(config_path)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return EXIT_CONFIG

    setup_logging(cfg.logs_dir, quiet=True)
    state = WebState(cfg, Path(config_path or "config.json"))
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    print()
    print("  畅课归档已启动")
    print(f"  地址：{url}")
    print("  关掉这个窗口即可停止服务")
    print()

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止")
    finally:
        server.server_close()
    return EXIT_OK
