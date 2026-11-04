"""下载：把清单里的条目一个个落到磁盘。

一个循环，带并发和进度回调。失败只记账，不中断整批。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .api import Api
from .client import AuthExpired, HttpClient
from .collect import Store

_log = logging.getLogger("download")

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_name(name: str, max_length: int = 96) -> str:
    """清洗成 Windows / macOS 都能落盘的文件名。"""
    name = _ILLEGAL.sub("_", name.replace("\u3000", " ")).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        return "未命名"
    stem, dot, suffix = name.rpartition(".")
    if not dot or len(suffix) > 12:
        stem, suffix = name, ""
    if stem.lower() in _RESERVED:
        stem = f"_{stem}"
    keep = max_length - (len(suffix) + 1 if suffix else 0)
    stem = stem[:keep].rstrip()
    return f"{stem}.{suffix}" if suffix else stem


def target_path(root: Path, course: str, name: str, used: set[str]) -> Path:
    """算出落点，同名自动加序号。"""
    folder = root / safe_name(course, 60)
    base = folder / safe_name(name)
    candidate = base
    counter = 2
    while str(candidate).lower() in used:
        candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        counter += 1
    used.add(str(candidate).lower())
    return candidate


def run(
    api: Api,
    http: HttpClient,
    root: Path,
    items: list[dict],
    store: Store,
    concurrency: int = 3,
    max_mb: int = 800,
    progress=None,
    log=None,
) -> dict:
    """items 是采集清单里挑出来的条目。返回统计结果。"""
    used: set[str] = set()
    pending: list[tuple[dict, Path]] = []

    for item in items:
        dest = target_path(root, item["course"], item["name"], used)
        saved = store.done.get(item["key"])
        if saved and Path(saved.get("path") or "").is_file():
            continue
        if dest.exists() and dest.stat().st_size > 0:
            store.mark_done(item["key"], dest, dest.stat().st_size)
            continue
        pending.append((item, dest))

    result = {"downloaded": 0, "skipped": len(items) - len(pending), "failed": [], "failed_keys": [], "bytes": 0}
    if not pending:
        store.save()
        return result

    max_bytes = max_mb * 1024 * 1024
    done = 0

    def fetch(item: dict, dest: Path) -> int:
        if log:
            log("INFO", f"开始下载：{item['name']} ({item['size'] / 1024 / 1024:.2f} MB)")
        try:
            res = http.download(api.download_url(item), dest, max_bytes=max_bytes)
            store.mark_done(item["key"], res.path, res.size)
            if log:
                log("INFO", f"下载完成：{item['name']} ({res.size / 1024 / 1024:.2f} MB)")
            return res.size
        except Exception as exc:
            if log:
                log("ERROR", f"下载失败：{item['name']} - {exc}")
            raise

    with ThreadPoolExecutor(max_workers=min(concurrency, len(pending))) as pool:
        futures = {pool.submit(fetch, item, dest): (item, dest) for item, dest in pending}
        for future in as_completed(futures):
            item, dest = futures[future]
            done += 1
            try:
                size = future.result()
            except AuthExpired:
                for f in futures:
                    f.cancel()
                raise
            except Exception as exc:
                result["failed"].append((item["name"], str(exc)[:160]))
                result["failed_keys"].append(item["key"])
                _log.error("失败 %s：%s", item["name"], exc)
            else:
                result["downloaded"] += 1
                result["bytes"] += size
                _log.info("完成 %s（%.2f MB）", dest.name, size / 1048576)
            if progress:
                progress(done, len(pending), item["name"])

    store.save()
    return result
