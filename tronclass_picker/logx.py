"""日志初始化：控制台 + 按天滚动文件。"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s"
_DATE_FMT = "%H:%M:%S"

_configured = False
_file_handler_path: Path | None = None


class _ColorFormatter(logging.Formatter):
    """仅在支持 ANSI 的终端上着色，避免日志文件里混入转义序列。"""

    COLORS = {
        logging.DEBUG: "\033[38;5;244m",
        logging.INFO: "\033[0m",
        logging.WARNING: "\033[38;5;214m",
        logging.ERROR: "\033[38;5;203m",
        logging.CRITICAL: "\033[1;38;5;203m",
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool) -> None:
        super().__init__(_FMT, _DATE_FMT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{text}{self.RESET}" if color else text


def setup(log_dir: Path, level: int = logging.INFO, quiet: bool = False) -> Path:
    """配置根 logger，返回本次运行的日志文件路径。

    重复调用是安全的：控制台句柄只建一次；输出目录变了就把文件句柄换掉，
    这样同进程里切换 --output 也能写到正确的位置。
    """
    global _configured, _file_handler_path
    _make_streams_forgiving()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run-{datetime.now():%Y%m%d}.log"
    console_level = logging.WARNING if quiet else level

    if _file_handler_path != log_file:
        for handler in list(root.handlers):
            if isinstance(handler, RotatingFileHandler):
                root.removeHandler(handler)
                handler.close()
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FMT + " | %(filename)s:%(lineno)d"))
        root.addHandler(file_handler)
        _file_handler_path = log_file

    if not _configured:
        use_color = sys.stderr.isatty() and (
            sys.platform != "win32" or _win_ansi_ok()
        )
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(console_level)
        console.setFormatter(_ColorFormatter(use_color))
        root.addHandler(console)
        _configured = True
        logging.getLogger("urllib3").setLevel(logging.WARNING)
    else:
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, RotatingFileHandler
            ):
                handler.setLevel(console_level)

    return log_file



def _make_streams_forgiving() -> None:
    """控制台代码页是 GBK 时，✓ 这类字符会抛 UnicodeEncodeError。

    把输出流的错误策略改成 replace，宁可显示成问号也别让整轮任务崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _win_ansi_ok() -> bool:
    """Windows 10+ 的 conhost 默认支持 VT 序列，其余情况保守关闭。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(mode.value & 0x0004)
    except Exception:
        return False


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
