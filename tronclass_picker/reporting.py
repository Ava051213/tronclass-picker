"""结果输出层：把 JobResult 变成人能读的摘要。

单独一层是为了让「怎么跑」和「怎么讲」解耦——以后想加 JSON 输出或推送到别处，
只改这里，执行链路不用动。
"""

from __future__ import annotations

import logging

from .models import JobResult

log = logging.getLogger("report")


def log_summary(result: JobResult) -> None:
    if result.planned_files:
        log.info(
            "文档：新下载 %d，跳过 %d，失败 %d，共 %.2f MB",
            result.downloaded,
            result.skipped,
            len(result.failed),
            result.bytes / 1048576,
        )

    if result.notice_file:
        log.info(
            "通知：新增 %d 条（平台 %d / 课程 %d），已写入 %s",
            result.notice_new,
            result.notice_by_scope.get("platform", 0),
            result.notice_by_scope.get("course", 0),
            result.notice_file,
        )
    elif result.notice_total:
        log.info("通知：本轮无新增（累计已汇总 %d 条）", result.notice_total)

    if result.failed:
        log.warning("以下文件下载失败，重新运行可补下：")
        for failure in result.failed[:20]:
            log.warning("  · %s：%s", failure.name, failure.reason)
        if len(result.failed) > 20:
            log.warning("  …… 其余 %d 个略", len(result.failed) - 20)

    log.info(
        "用时 %.1fs；请求 %d 次（重试 %d 次），传输 %.2f MB",
        result.elapsed,
        result.requests,
        result.retries,
        result.bytes / 1048576,
    )


def console_summary(result: JobResult) -> str:
    """给交互模式用的一行式结论。"""
    parts = []
    if result.downloaded:
        parts.append(f"下载 {result.downloaded} 个文件")
    if result.skipped:
        parts.append(f"跳过 {result.skipped} 个")
    if result.failed:
        parts.append(f"{len(result.failed)} 个失败")
    if result.notice_new:
        parts.append(f"{result.notice_new} 条新通知")
    return "，".join(parts) if parts else "没有需要处理的内容"
