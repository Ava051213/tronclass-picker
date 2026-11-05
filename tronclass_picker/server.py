"""本地服务：一个页面 + 六个接口，默认在 127.0.0.1:8765 上。

网页只读 /api/state 这一份快照，动作靠 POST 触发，后台线程干活。

给外部程序留了一个口子：GET /api/todos，每次都现拉平台接口（约 1 秒），
不依赖扫描是否跑过，也不用等那套几十个请求的流程。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from . import download, login
from .api import Api
from .client import AuthExpired, HttpClient, TronClassError
from .collect import Store, scan
from .config import Config, dump_default, load

log = logging.getLogger("server")

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}

INDEX = Path(__file__).parent / "web" / "index.html"


class State:
    """全部可变状态集中在这里，读写都过锁。"""

    def __init__(self, cfg: Config, config_path: Path) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.lock = threading.Lock()

        # 建连接和拉待办各自一条锁：外部接口会并发打进来，
        # 不能两个线程同时去登录态里摸 HttpClient。
        self._connect_lock = threading.RLock()
        self._todos_lock = threading.Lock()
        self._todos_cache: list[dict] = []
        self._todos_at = 0.0

        # 公告正文按 id 缓存：公告基本不变，命中就不再一条条向平台要
        self._notice_bodies: dict[str, str] = {}

        # 快照序列化缓存：页面轮询 /api/state 很勤，状态没变（rev 不动）就复用上次的字节
        self._rev = 0
        self._snapshot_cache: tuple[int, bytes] | None = None

        # 日志缓冲：最多保留 500 条，旧的丢掉
        self._logs: list[dict] = []
        self._log_lock = threading.Lock()
        self._max_logs = 500

        self.http: HttpClient | None = None
        self.api: Api | None = None
        self.store = Store(cfg.state_file)

        self.user = ""
        self.courses: list[dict] = []
        self.files: list[dict] = []
        self.todos: list[dict] = []
        self.notices: list[dict] = []
        self.scope = "current"

        self.busy = False
        # 结构化状态：idle / scanning / downloading / logging_in / done / auth_expired / error。
        # 前端只拿它判断样式，status 文案随便改都不会弄坏界面状态。
        self.phase = "idle"
        self.status = "准备中…"
        self.done = 0
        self.total = 0
        self.current = ""
        self.synced = ""

    # ---- 日志 --------------------------------------------------------

    def add_log(self, level: str, message: str) -> None:
        """往环形缓冲里塞一条日志。"""
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {
            "ts": ts,
            "level": _LEVELS.get(level.upper(), 20),
            "name": level.upper(),
            "message": message,
            "text": f"{ts} {message}",
        }
        with self._log_lock:
            self._logs.append(entry)
            if len(self._logs) > self._max_logs:
                self._logs = self._logs[-self._max_logs :]
        with self.lock:
            self._bump()

    def get_logs(self) -> list[dict]:
        with self._log_lock:
            return list(self._logs)

    def clear_logs(self) -> None:
        with self._log_lock:
            self._logs.clear()
        with self.lock:
            self._bump()

    # ---- 快照 --------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            return self._build_snapshot()

    def snapshot_bytes(self) -> bytes:
        """/api/state 走这份：状态没动（rev 不变）就复用上次的序列化结果，
        空闲轮询从「每拍全量序列化」降为一次字节复用。"""
        with self.lock:
            if self._snapshot_cache is not None and self._snapshot_cache[0] == self._rev:
                return self._snapshot_cache[1]
            data = json.dumps(self._build_snapshot(), ensure_ascii=False).encode("utf-8")
            self._snapshot_cache = (self._rev, data)
            return data

    def _build_snapshot(self) -> dict:
        return {
            "user": self.user,
            "courses": len(self.courses),
            "courseCount": len(self.courses),
            "files": list(self.files),
            "todos": list(self.todos),
            "notices": list(self.notices),
            "busy": self.busy,
            "phase": self.phase,
            "status": self.status,
            "progress": {"done": self.done, "total": self.total},
            "current": self.current,
            "synced": self.synced,
            "scope": self.scope,
            "downloaded": [f["key"] for f in self.files if f.get("done")],
            "logs": self.get_logs(),
        }

    def _bump(self) -> None:
        """状态变了，快照缓存作废。"""
        self._rev += 1
        self._snapshot_cache = None

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self._bump()

    # ---- 连接 --------------------------------------------------------
    def connect(self) -> None:
        """建 HttpClient 并验证会话；已有连接就直接复用。"""
        with self._connect_lock:
            if self.api is not None:
                return
            http = HttpClient(self.cfg)
            api = Api(http)
            profile = api.profile()
            if not profile:
                raise AuthExpired("会话无效")
            name = (
                profile.get("name")
                or profile.get("display_name")
                or profile.get("username")
                or profile.get("login")
                or ""
            )
            with self.lock:
                self.http = http
                self.api = api
                self.user = str(name)

    def reload(self) -> None:
        """重读 config.json 并丢掉旧连接。

        登录只把新 Cookie 写进文件，内存里那份是旧的；外部接口掉登录后
        重新读一次，就能接上刚登录的会话。
        """
        try:
            cfg = load(self.config_path)
        except Exception as exc:
            log.warning("重读配置失败：%s", exc)
            return
        with self._connect_lock:
            self.cfg = cfg
            self.http = None
            self.api = None

    # ---- 待办接口 ----------------------------------------------------
    def live_todos(self, ttl: float = 3.0) -> list[dict]:
        """给外部程序用：现拉待办，不走整轮扫描。

        ttl 秒内重复调用直接给上次结果，防止小助手高频轮询把账号刷进风控。
        """
        with self._todos_lock:
            if self._todos_cache and time.monotonic() - self._todos_at < ttl:
                return list(self._todos_cache)
            try:
                todos = self._fetch_todos()
            except AuthExpired:
                self.reload()
                todos = self._fetch_todos()
            self._todos_cache, self._todos_at = list(todos), time.monotonic()
            return todos

    def _fetch_todos(self) -> list[dict]:
        try:
            self.connect()
            api = self.api
            assert api is not None
            todos = api.todos(strict=True)
            self.add_log("INFO", f"获取到 {len(todos)} 条待办")
            return todos
        except AuthExpired:
            with self._connect_lock:
                self.http = None
                self.api = None
            raise

    def _fill_notice_bodies(self, api: Api, notices: list[dict]) -> None:
        """给公告补正文。记过的直接用缓存，不再一条条向平台要（限速下 0.5s/条）。"""
        for n in notices:
            body = self._notice_bodies.get(n["id"])
            if body is None:
                body = _plain(api.notice_body(n))[:400]
                self._notice_bodies[n["id"]] = body
            n["body"] = body
        if len(self._notice_bodies) > 500:
            # 兜底：按插入序丢掉最老的一半，缓存别随年月无界涨
            for k in list(self._notice_bodies)[:250]:
                self._notice_bodies.pop(k, None)

    # ---- 动作 --------------------------------------------------------
    def start_scan(self, scope: str | None = None) -> None:
        if self.busy:
            return
        if scope in ("current", "all"):
            self.scope = scope
        self.set(busy=True, phase="scanning", status="正在读取课程…", done=0, total=0, current="")
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        try:
            self.add_log("INFO", "开始扫描课程")
            if self.api is None:
                self.connect()
            api = self.api
            assert api is not None

            courses = api.courses()
            self.set(courses=courses, status=f"正在扫描 {len(courses)} 门课…")
            self.add_log("INFO", f"获取到 {len(courses)} 门课程")

            def tick(i: int, n: int, name: str) -> None:
                self.set(done=i, total=n, current=name, status=f"正在扫描：{name}")
                self.add_log("INFO", f"正在扫描：{name}")

            scan_courses = scope_courses(courses, self.scope)
            _, files = scan(api, self.store, scan_courses, progress=tick)
            self.add_log("INFO", f"扫描到 {len(files)} 个文件")
            todos = api.todos()
            self.set(status="正在读取公告…", done=0, total=0, current="")

            notices = api.notices(scan_courses)
            self._fill_notice_bodies(api, notices)

            self.store.mark_known([f["key"] for f in files])
            self.store.save()

            self.set(
                files=files,
                todos=todos,
                notices=notices,
                busy=False,
                phase="done",
                status="扫描完成",
                current="",
                synced=datetime.now().strftime("%H:%M 同步"),
            )
            self.add_log("INFO", f"扫描完成：{len(files)} 文件 / {len(todos)} 待办 / {len(notices)} 公告")
            log.info("扫描完成：%d 文件 / %d 待办 / %d 公告", len(files), len(todos), len(notices))
        except AuthExpired:
            self.set(busy=False, phase="auth_expired", status="登录已失效，点右上角登录", current="")
            self.add_log("ERROR", "登录已失效，点右上角登录")
        except Exception as exc:
            log.exception("扫描失败")
            self.set(busy=False, phase="error", status=f"扫描失败：{exc}", current="")
            self.add_log("ERROR", f"扫描失败：{exc}")

    def start_notices(self) -> None:
        if self.busy:
            return
        self.set(busy=True, phase="scanning", status="正在读取公告…", done=0, total=0, current="")
        threading.Thread(target=self._notices, daemon=True).start()

    def _notices(self) -> None:
        try:
            if self.api is None:
                self.connect()
            api = self.api
            assert api is not None
            courses = self.courses or api.courses()
            targets = scope_courses(courses, self.scope)
            notices = api.notices(targets)
            self._fill_notice_bodies(api, notices)
            self.set(notices=notices, busy=False, phase="done", status=f"公告已更新（{len(notices)} 条）")
            self.add_log("INFO", f"公告已更新：{len(notices)} 条")
        except AuthExpired:
            self.set(busy=False, phase="auth_expired", status="登录已失效，点右上角登录")
            self.add_log("ERROR", "登录已失效，点右上角登录")
        except Exception as exc:
            log.exception("公告读取失败")
            self.set(busy=False, phase="error", status=f"公告读取失败：{exc}")
            self.add_log("ERROR", f"公告读取失败：{exc}")

    def start_download(self, keys: list[str]) -> None:
        if self.busy:
            return
        with self.lock:
            picked = [f for f in self.files if f["key"] in set(keys)]
        if not picked:
            self.add_log("INFO", "没有需要下载的文件")
            return
        self.set(busy=True, phase="downloading", status="准备下载…", done=0, total=len(picked), current="")
        threading.Thread(target=self._download, args=(picked,), daemon=True).start()
        self.add_log("INFO", f"准备下载 {len(picked)} 个文件")

    def _download(self, picked: list[dict]) -> None:
        try:
            if self.api is None:
                self.connect()
            api = self.api
            http = self.http
            assert api is not None and http is not None

            def log_callback(level: str, message: str) -> None:
                self.add_log(level, message)

            result = download.run(
                api,
                http,
                self.cfg.documents_dir,
                picked,
                self.store,
                concurrency=self.cfg.documents.concurrency,
                max_mb=self.cfg.documents.max_size_mb,
                progress=lambda i, n, name: self.set(done=i, total=n, current=name),
                log=log_callback,
            )
            tail = (
                f"完成 {result['downloaded']}，跳过 {result['skipped']}，"
                f"失败 {len(result['failed'])}（{result['bytes'] / 1048576:.1f} MB）"
            )
            if result["failed"]:
                tail += "　首个失败：" + result["failed"][0][1][:60]

            bad = set(result.get("failed_keys") or [])
            done_keys = {f["key"] for f in picked} - bad
            with self.lock:
                for f in self.files:
                    if f["key"] in done_keys:
                        f["done"] = True
            self.set(busy=False, phase="done", status=tail, current="", done=len(picked), total=len(picked))
            self.add_log("INFO", f"下载完成：{tail}")
        except AuthExpired:
            self.set(busy=False, phase="auth_expired", status="登录已失效，点右上角登录", current="")
            self.add_log("ERROR", "登录已失效，点右上角登录")
        except Exception as exc:
            log.exception("下载失败")
            self.set(busy=False, phase="error", status=f"下载失败：{exc}", current="")
            self.add_log("ERROR", f"下载失败：{exc}")

    def start_login(self) -> None:
        if self.busy:
            return
        self.set(busy=True, phase="logging_in", status="已打开浏览器，请完成统一认证…", done=0, total=0, current="")
        threading.Thread(target=self._login, daemon=True).start()
        self.add_log("INFO", "已打开浏览器，请完成统一认证…")

    def _login(self) -> None:
        try:
            self.add_log("INFO", "开始登录")
            code = login.run(self.cfg, self.config_path, wait=login.wait_for_login)
        except Exception as exc:
            log.exception("登录失败")
            self.set(busy=False, phase="error", status=f"登录失败：{exc}")
            self.add_log("ERROR", f"登录失败：{exc}")
            return
        if code != 0:
            self.set(busy=False, phase="idle", status="登录没有完成，可以再试一次")
            self.add_log("WARN", "登录没有完成，可以再试一次")
            return
        self.reload()
        self.set(busy=False, phase="idle", status="登录成功，开始扫描…")
        self.start_scan()


def scope_courses(courses: list[dict], scope: str | None) -> list[dict]:
    """按 scope 过滤课程：all 返回全部，current 只取最近学期，None 默认 current。"""
    if scope == "all":
        return courses
    return _current_term(courses)


def _current_term(courses: list[dict]) -> list[dict]:
    """只取最近一个学期的课。公告按课程请求，扫历史课纯属白跑。"""
    names = [c["semester"] for c in courses if c.get("semester")]
    if not names:
        return courses
    latest = max(names)
    return [c for c in courses if c.get("semester") == latest] or courses


def _plain(html: str) -> str:
    """粗洗公告正文：去标签、压空行，够看清内容就行。"""
    import re

    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    )
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def decorate(todos: list[dict], now: datetime | None = None) -> list[dict]:
    """给外部调用方补上剩余小时数和过期标记，省得对面自己算时间。"""
    now = now or datetime.now()
    out = []
    for item in todos:
        row = dict(item)
        when = None
        if item.get("due_ts"):
            try:
                when = datetime.strptime(item["due_ts"], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                when = None
        if when is None:
            row["hours_left"] = None
            row["overdue"] = False
        else:
            row["hours_left"] = round((when - now).total_seconds() / 3600, 1)
            row["overdue"] = when < now
        out.append(row)
    return out


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *args) -> None:
        pass

    def _cors_ok(self) -> bool:
        """浏览器跨域才带 Origin：curl / 外部程序不带，WebView 的 file:// 页面是 null。

        只放行本机来源。JSON POST 必走预检，拿不到 ACAO 头浏览器直接拦——
        别的网页就读不到待办、也触发不了下载了。
        """
        origin = self.headers.get("Origin")
        if origin in (None, "null"):
            return True
        return (urlsplit(origin).hostname or "") in ("127.0.0.1", "localhost")

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 外部程序多半跑在浏览器/WebView 里，跨域这几行要给，但只给本机来源
        if self._cors_ok():
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

    def do_OPTIONS(self) -> None:
        self._send(b"", "text/plain; charset=utf-8", 204)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path

        # 静态资源
        if path.startswith("/static/"):
            root = (Path(__file__).resolve().parent / "static").resolve()
            target = (root / path[len("/static/"):]).resolve()
            if not target.is_relative_to(root):
                self._send(b"forbidden", "text/plain; charset=utf-8", 403)
                return
            if target.is_file():
                self._send(target.read_bytes(), self._mime_type(target), 200)
                return
            self._send(f"静态文件缺失：{path}".encode(), "text/plain; charset=utf-8", 404)
            return

        if path in ("/", "/index.html"):
            try:
                html = INDEX.read_text(encoding="utf-8")
            except OSError as exc:
                self._send(f"页面缺失：{exc}".encode(), "text/plain; charset=utf-8", 500)
                return
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(self.state.snapshot_bytes(), "application/json; charset=utf-8")
        elif path == "/api/todos":
            self._todos()
        elif path == "/api/logs":
            self._logs()
        elif path in ("/api/health", "/health"):
            self._json({"ok": True, "service": "tronclass-picker", "todos": "/api/todos"})
        else:
            self._json({"error": "not found"}, 404)

    def _mime_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            return "text/html; charset=utf-8"
        if suffix in (".css"):
            return "text/css; charset=utf-8"
        if suffix in (".js"):
            return "application/javascript; charset=utf-8"
        if suffix in (".json"):
            return "application/json; charset=utf-8"
        return "application/octet-stream"

    def _logs(self) -> None:
        self._json({"ok": True, "logs": self.state.get_logs()}, 200)

    def _todos(self) -> None:
        """外部程序要的就是这一个：现拉一次，返回最新待办。"""
        try:
            todos = self.state.live_todos()
        except AuthExpired as exc:
            self._json(
                {
                    "ok": False,
                    "error": "auth_expired",
                    "message": f"{exc}；打开网页点右上角登录",
                },
                401,
            )
            return
        except TronClassError as exc:
            self._json({"ok": False, "error": "upstream", "message": str(exc)}, 502)
            return
        except Exception as exc:  # 兜底，别让外部调用方等到超时
            log.exception("待办接口异常")
            self._json({"ok": False, "error": "internal", "message": str(exc)}, 500)
            return
        items = decorate(todos)
        self._json({
            "ok": True,
            "count": len(items),
            "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "todos": items,
        })

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return

        if path == "/api/scan":
            self.state.start_scan()
            self._json({"ok": True})
        elif path == "/api/reload":
            scope = payload.get("scope")
            self.state.start_scan(scope if scope in ("current", "all") else None)
            self._json({"ok": True})
        elif path == "/api/notices":
            self.state.start_notices()
            self._json({"ok": True})
        elif path == "/api/login":
            self.state.start_login()
            self._json({"ok": True})
        elif path == "/api/download":
            keys = payload.get("keys") or []
            if not isinstance(keys, list):
                self._json({"error": "keys 必须是数组"}, 400)
                return
            self.state.start_download([str(k) for k in keys])
            self._json({"ok": True})
        elif path == "/api/logs/clear":
            self.state.clear_logs()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def _already_running(host: str, port: int) -> bool:
    """端口上已经有本工具在跑就别起第二个。

    否则第二个实例会漂到随机端口，外部程序按 8765 找过来就断了。
    显式绕过系统代理——这台机器挂着 Clash，走代理会把本机请求送出去。
    """
    if not port:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{host}:{port}/api/health", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return False
    return isinstance(data, dict) and data.get("service") == "tronclass-picker"


def serve(
    config_path: str | Path | None = None,
    *,
    open_browser: bool = True,
    port: int | None = None,
    auto_scan: bool = True,
    log_path: str | Path | None = None,
) -> int:
    if log_path:
        target = Path(log_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        # 常驻服务的日志要封顶：2MB 滚一个备份，最多留两个，别让 service.log 无限涨
        handler = RotatingFileHandler(
            target, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logging.basicConfig(level=logging.INFO, handlers=[handler])

    p = Path(config_path or "config.json")
    if not p.exists():
        dump_default(p)
        print(f"已生成配置模板：{p}")

    cfg = load(p)
    state = State(cfg, p)
    Handler.state = state

    host = cfg.server.host or "127.0.0.1"
    want = int(port or cfg.server.port or 0)
    if _already_running(host, want):
        print(f"  {host}:{want} 上已经有一个在跑了，不再重复启动")
        print(f"  页面      http://{host}:{want}/")
        print(f"  待办接口  http://{host}:{want}/api/todos")
        return 0
    try:
        server = ThreadingHTTPServer((host, want), Handler)
    except OSError as exc:
        # 固定端口被占了不能让外部程序找不到，先明确喊一声再退到随机端口
        print(f"  端口 {want} 起不来（{exc}），改用随机端口")
        server = ThreadingHTTPServer((host, 0), Handler)

    bound = server.server_address[1]
    url = f"http://{host}:{bound}/"
    log.info("服务已启动 %s", url)

    print()
    print("  畅课归档已启动")
    print(f"  页面      {url}")
    print(f"  待办接口  {url}api/todos")
    print("  关掉这个窗口就停止")
    print()

    if auto_scan:
        threading.Timer(0.5, state.start_scan).start()
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止")
    finally:
        server.server_close()
    return 0
