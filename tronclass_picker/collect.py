"""采集：把账号里的课程文件抓成一份扁平清单，顺带记住哪些是新的。

这一层只认 Api 和 Store，不碰磁盘写入、不碰下载。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .api import Api

log = logging.getLogger("collect")


class Store:
    """记录「见过哪些文件」和「下过哪些文件」，用于标新增和跳过重复下载。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.known: set[str] = set()
        self.done: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("状态文件读不出来，按首次运行处理")
            return
        if isinstance(data, dict):
            self.known = set(data.get("known") or [])
            done = data.get("done")
            if isinstance(done, dict):
                self.done = {k: v for k, v in done.items() if isinstance(v, dict)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"known": sorted(self.known), "done": self.done}
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("状态文件写入失败：%s", exc)

    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark_done(self, key: str, path: Path, size: int) -> None:
        self.done[key] = {"path": str(path), "size": size}

    def mark_known(self, keys: list[str]) -> None:
        self.known.update(keys)


def scan(
    api: Api,
    store: Store,
    courses: list[dict] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """拉课程 + 每门课的附件，返回 (课程, 文件清单)。

    文件清单里每项都带 isNew（上次没见过的）和 done（本地已有）。
    """
    if courses is None:
        courses = api.courses()

    first_run = not store.known      # 首次没有基线，全部算老文件，免得满屏「新增」
    files: list[dict] = []
    total = len(courses)
    for index, course in enumerate(courses, start=1):
        try:
            attachments = list(api.attachments(course["id"]))
        except Exception as exc:
            log.warning("%s 读取失败：%s", course["name"], exc)
            attachments = []

        for att in attachments:
            key = f"{course['id']}:{att['ref']}"
            files.append({
                "key": key,
                "course": course["name"],
                "course_id": course["id"],
                "name": att["name"],
                "size": att["size"],
                "date": att.get("date") or "",
                "source": att["source"],
                "restricted": not att["allow"],
                "isNew": (not first_run) and key not in store.known,
                "done": store.is_done(key),
                "ref": att["ref"],
                "upload_id": att["upload_id"],
                "url": att["url"],
            })
        if progress:
            progress(index, total, course["name"])

    log.info("采集完成：%d 门课，%d 个文件", len(courses), len(files))
    return courses, files
