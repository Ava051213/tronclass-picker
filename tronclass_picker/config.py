"""配置加载与校验。

配置来源优先级（后者覆盖前者）：
    内置默认值  <  config.json  <  环境变量（TRONCLASS_*）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://lms.szu.edu.cn"

# 默认归档的文档扩展名。可在 config.json 的 documents.extensions 覆盖。
# 图片（.png/.jpg）与可执行文件默认不收：前者多是封面缩略图，后者有安全风险。
DEFAULT_EXTENSIONS = [
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".txt", ".md", ".rtf", ".odt", ".odp", ".ods",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".epub", ".mobi", ".caj",
    ".py", ".java", ".c", ".cpp", ".h", ".chisel", ".scala", ".ipynb",
    ".mp3", ".mp4", ".m4a", ".wav",   # 录播/音频讲义；不需要可自行删除
]

# 视为「课程文档」的活动类型。不同租户命名略有差异，这里做并集匹配。
DOC_ACTIVITY_TYPES = {
    "material", "document", "file", "courseware", "attachment",
    "resource", "reading", "ppt", "pdf", "video", "audio", "web_link",
}


class ConfigError(Exception):
    """配置文件不合法。"""


@dataclass
class AuthConfig:
    cookie: str = ""            # 形如 "sessionid=xxx; csrftoken=yyy"
    cookie_file: str = ""       # Netscape 格式 cookies.txt 路径
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class PathsConfig:
    output: str = "./output"
    documents: str = "documents"
    notices: str = "notices"
    logs: str = "logs"


@dataclass
class DocumentsConfig:
    enabled: bool = True
    extensions: list[str] = field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    include_coursewares: bool = False    # /coursewares 返回的就是 material 活动，默认走 activities 足够
    include_activity_refs: bool = True   # 活动自带的 uploads（课件、讲义、附件）
    include_restricted: bool = False     # 是否尝试下载 allow_download=false 的文件
    skip_existing: bool = True           # 依据 .state.json 跳过已下载
    max_size_mb: int = 800               # 单文件大小上限，0 表示不限制
    concurrency: int = 3                 # 并发下载数；平台有风控，别调太高
    organize_by: str = "course"          # course | semester | flat


@dataclass
class NoticesConfig:
    enabled: bool = True
    days: int = 30                       # 只汇总最近 N 天；0 表示全量
    all_courses: bool = True             # 忽略 courses 的学期/关键词筛选，扫全部课程
    include_platform: bool = True        # 平台级公告 /api/announcement
    include_course: bool = True          # 课程内公告
    include_content: bool = True         # 抓取公告正文（多一次请求）
    max_per_course: int = 50
    output: str = "both"                 # log（只进日志）| markdown（只写文件）| both


@dataclass
class CoursesConfig:
    include: list[str] = field(default_factory=list)   # 名称或 ID 关键字，空表示全部
    exclude: list[str] = field(default_factory=list)
    semester: str = ""                                 # 关键字过滤学期，如 "2026-2027"


@dataclass
class NetworkConfig:
    timeout: int = 30
    retries: int = 3
    retry_backoff: float = 2.0
    request_interval: float = 0.5        # 每次请求之间的最小间隔（秒）
    verify_tls: bool = True


@dataclass
class RunConfig:
    interval: str = ""                   # 如 "6h"、"30m"；留空表示单次执行
    max_runs: int = 0                    # 定时模式下最多执行几轮，0 表示不限


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    auth: AuthConfig = field(default_factory=AuthConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    documents: DocumentsConfig = field(default_factory=DocumentsConfig)
    notices: NoticesConfig = field(default_factory=NoticesConfig)
    courses: CoursesConfig = field(default_factory=CoursesConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    run: RunConfig = field(default_factory=RunConfig)

    # ---- 派生路径 ----------------------------------------------------
    @property
    def output_dir(self) -> Path:
        return Path(self.paths.output).expanduser().resolve()

    @property
    def documents_dir(self) -> Path:
        return self.output_dir / self.paths.documents

    @property
    def notices_dir(self) -> Path:
        return self.output_dir / self.paths.notices

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / self.paths.logs

    @property
    def state_file(self) -> Path:
        return self.output_dir / ".state.json"


def load(path: str | Path | None, overrides: dict[str, Any] | None = None) -> Config:
    """读取配置文件。

    path 为 None 时自动查找当前目录的 config.json（找不到就用纯默认值）；
    显式指定的路径不存在则直接报错，避免路径写错却静默跑默认配置。
    """
    raw: dict[str, Any] = {}
    explicit = path is not None
    p = Path(path).expanduser() if explicit else Path("config.json")
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{p} 不是合法的 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{p} 的顶层必须是 JSON 对象")
    elif explicit:
        raise ConfigError(f"找不到配置文件：{p}")

    _apply_env(raw)
    if overrides:
        _deep_merge(raw, overrides)

    cfg = _build(Config, raw)
    _validate(cfg)
    return cfg


def _apply_env(raw: dict[str, Any]) -> None:
    """少量关键项支持环境变量，便于在任务计划里免配置切换。"""
    env_map = {
        "TRONCLASS_BASE_URL": ("base_url",),
        "TRONCLASS_COOKIE": ("auth", "cookie"),
        "TRONCLASS_COOKIE_FILE": ("auth", "cookie_file"),
        "TRONCLASS_OUTPUT": ("paths", "output"),
    }
    for env_key, keys in env_map.items():
        value = os.environ.get(env_key)
        if not value:
            continue
        node = raw
        for key in keys[:-1]:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                break
        else:
            node[keys[-1]] = value


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        elif value is not None:
            base[key] = value


def _build(cls: type, data: dict[str, Any]) -> Any:
    """按 dataclass 定义把 dict 递归转换为实例，忽略未知键。"""
    instance = cls()
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(instance, f.name)
        if is_dataclass(current) and isinstance(value, dict):
            setattr(instance, f.name, _build(type(current), value))
        else:
            setattr(instance, f.name, value)
    return instance


def _validate(cfg: Config) -> None:
    if not cfg.base_url.startswith(("http://", "https://")):
        raise ConfigError("base_url 必须以 http:// 或 https:// 开头")
    cfg.base_url = cfg.base_url.rstrip("/")

    if cfg.documents.organize_by not in ("course", "semester", "flat"):
        raise ConfigError("documents.organize_by 只能是 course / semester / flat")
    if cfg.documents.concurrency < 1 or cfg.documents.concurrency > 8:
        raise ConfigError("documents.concurrency 建议在 1~8 之间")
    if cfg.network.request_interval < 0:
        raise ConfigError("network.request_interval 不能为负数")
    if cfg.notices.days < 0:
        raise ConfigError("notices.days 不能为负数")

    cfg.documents.extensions = [
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in cfg.documents.extensions
    ]


def dump_default(path: str | Path) -> Path:
    """把当前内置默认值写成示例配置。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(_to_dict(Config()), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return p


def _to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [_to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj
