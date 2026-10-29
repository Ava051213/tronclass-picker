"""增量状态记录，用于跳过已下载的文件和已汇总的通知。"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("state")


class StateStore:
    """极简 JSON 状态库。写入采用「临时文件 + 替换」，避免中途崩溃写坏。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"version": 1, "documents": {}, "notices": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._data.update({
                    "version": data.get("version", 1),
                    "documents": data.get("documents") or {},
                    "notices": data.get("notices") or {},
                })
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("状态文件损坏，将重建：%s", exc)

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)

    # ---- 文档 --------------------------------------------------------
    def has_document(self, key: str) -> bool:
        """已记录且文件仍在磁盘上才算真正下载过。"""
        record = self._data["documents"].get(key)
        if not record:
            return False
        stored = record.get("path")
        return bool(stored and Path(stored).exists())

    def mark_document(self, key: str, path: Path, size: int, sha1: str) -> None:
        with self._lock:
            self._data["documents"][key] = {
                "path": str(path),
                "size": size,
                "sha1": sha1,
                "at": datetime.now().isoformat(timespec="seconds"),
            }

    def document_count(self) -> int:
        return len(self._data["documents"])

    # ---- 通知 --------------------------------------------------------
    def notice_seen(self, key: str) -> bool:
        return key in self._data["notices"]

    def mark_notice(self, key: str) -> None:
        with self._lock:
            self._data["notices"][key] = datetime.now().isoformat(timespec="seconds")

    def notice_count(self) -> int:
        return len(self._data["notices"])
