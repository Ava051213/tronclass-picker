"""配置：一个 config.json，读进来就是几个分组，别的不猜。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://lms.szu.edu.cn"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class ConfigError(Exception):
    """配置文件不合法。"""


@dataclass
class Auth:
    cookie: str = ""
    cookie_file: str = ""
    user_agent: str = DEFAULT_UA
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Paths:
    output: str = "./output"
    documents: str = "documents"


@dataclass
class Documents:
    concurrency: int = 3
    max_size_mb: int = 800


@dataclass
class Network:
    timeout: int = 30
    retries: int = 3
    retry_backoff: float = 2.0
    request_interval: float = 0.5


@dataclass
class Server:
    """本地服务的监听地址。外部程序靠这个固定端口找过来，别随便改。

    只监听 127.0.0.1，别的机器连不上；要跨设备再换成 0.0.0.0。
    """

    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    auth: Auth = field(default_factory=Auth)
    paths: Paths = field(default_factory=Paths)
    documents: Documents = field(default_factory=Documents)
    network: Network = field(default_factory=Network)
    server: Server = field(default_factory=Server)

    @property
    def output_dir(self) -> Path:
        return Path(self.paths.output).expanduser().resolve()

    @property
    def documents_dir(self) -> Path:
        return self.output_dir / self.paths.documents

    @property
    def state_file(self) -> Path:
        return self.output_dir / ".state.json"

    @property
    def config_path(self) -> Path:
        return Path("config.json")


def load(path: str | Path | None = None) -> Config:
    raw: dict[str, Any] = {}
    explicit = path is not None
    p = Path(path).expanduser() if explicit else Path("config.json")
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{p} 不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{p} 顶层必须是对象")
    elif explicit:
        raise ConfigError(f"找不到配置文件：{p}")

    cfg = _build(Config, raw)
    if not cfg.base_url.startswith(("http://", "https://")):
        raise ConfigError("base_url 要以 http:// 或 https:// 开头")
    cfg.base_url = cfg.base_url.rstrip("/")
    return cfg


def _build(cls: type, data: dict[str, Any]) -> Any:
    """按 dataclass 定义递归转换，未声明的键直接丢掉。"""
    instance = cls()
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(instance, f.name)
        if is_dataclass(current) and isinstance(value, dict):
            setattr(instance, f.name, _build(type(current), value))
        elif value is not None:
            setattr(instance, f.name, value)
    return instance


def dump_default(path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_plain(Config()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return p


def _plain(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj
