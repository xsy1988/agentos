"""run 时长账本与全局截止时间（方案 §4 P0-1）。

设计要点：
- 超时以**绝对截止时间** `deadline_at` 表达，一次写入、永不重置；每次执行段只取
  "到截止点还剩多少秒"喂给 `wait_for`，因此 interrupt/resume 分段累计不会漂移。
- 暂停（等确认/等外部）**顺延**截止点，但顺延量受 `max_pause_seconds` 封顶，
  避免"用户第二天才确认"使保护失效。
- 本模块为纯函数：不碰 DB、不碰 I/O，`now` 一律由调用方注入（运行时统一取 UTC），
  便于单测直接覆盖核心不变量。
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

# 结果信封版本（方案 §4 P0-5；P0-1 先用于"超时也有结果"）
RUN_RESULT_SCHEMA = "run_result/v1"

# budget.timeout_seconds 全链路缺省值（run 快照 → Agent 配置 → 此处）
DEFAULT_TIMEOUT_SECONDS = 600


def resolve_timeout_seconds(
    budget: dict | None,
    agent_timeout: int | None = None,
    default: int = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """超时秒数解析链：run.budget 快照 → Agent 配置 → 缺省。

    运行中改 Agent 配置不影响已创建的 run（budget 是创建时固化的快照）。
    """
    for candidate in ((budget or {}).get("timeout_seconds"), agent_timeout, default):
        if candidate:
            value = int(candidate)
            if value > 0:
                return value
    return default


def compute_deadline(started_at: datetime, timeout_seconds: int) -> datetime:
    """首次进入 running 时算一次，之后只顺延（暂停）不重算。"""
    return started_at + timedelta(seconds=timeout_seconds)


def remaining_seconds(deadline_at: datetime, now: datetime) -> float:
    """到截止点还剩多少秒；≤ 0 表示执行预算已耗尽，不应再调用图。"""
    return (deadline_at - now).total_seconds()


def pause_extension(paused_at: datetime, now: datetime, max_pause_seconds: int) -> float:
    """暂停期间应顺延的秒数（封顶）。

    返回顺延量；调用方据此 `deadline_at += extension`。封顶后剩余顺延量被丢弃，
    即"暂停过长时不再无限保护"，run 会按耗尽如实收尾。
    """
    paused = max(0.0, (now - paused_at).total_seconds())
    if max_pause_seconds > 0:
        return min(paused, float(max_pause_seconds))
    return paused


def accumulate_active_ms(
    active_ms: int | None, segment_started_at: datetime | None, now: datetime
) -> int:
    """把当前执行段的时长累加到账本；已结束的段只累加一次。"""
    total = int(active_ms or 0)
    if segment_started_at is None:
        return total
    return total + max(0, int((now - segment_started_at).total_seconds() * 1000))


def elapsed_ms(started_at: datetime | None, end: datetime) -> int:
    """总存活时长（含暂停/等待），与 active_ms 的差额即"非执行时长"。"""
    if started_at is None:
        return 0
    return max(0, int((end - started_at).total_seconds() * 1000))


def budget_metrics(
    budget_used: Mapping[str, Any] | None, *, elapsed_ms: int, active_ms: int
) -> dict[str, int]:
    """结果信封 metrics：复用 budget_used，不新造账本。"""
    used: Mapping[str, Any] = budget_used or {}
    return {
        "elapsed_ms": elapsed_ms,
        "active_ms": active_ms,
        "iterations": int(used.get("iterations") or 0),
        "tool_calls": int(used.get("tool_calls") or 0),
        "input_tokens": int(used.get("input_tokens") or 0),
        "output_tokens": int(used.get("output_tokens") or 0),
    }


def partial_result(
    *,
    text: str,
    reason: str,
    metrics: dict[str, int],
    deadline_at: datetime | None = None,
) -> dict:
    """超时/中断时的结果信封：**失败也有结果**，不再 `result = NULL`。"""
    return {
        "schema": RUN_RESULT_SCHEMA,
        "outcome": "partial",
        "partial": True,
        "reason": reason,
        "text": text,
        "metrics": metrics,
        "deadline_at": deadline_at.isoformat() if deadline_at else None,
    }


def failure_result(
    *,
    text: str,
    reason: str,
    metrics: dict[str, int],
    extra: Mapping[str, Any] | None = None,
) -> dict:
    """硬失败（如工具连续失败熔断）的结果信封：非 partial、带失败原因与上下文。"""
    out: dict[str, Any] = {
        "schema": RUN_RESULT_SCHEMA,
        "outcome": "failed",
        "partial": False,
        "reason": reason,
        "text": text,
        "metrics": metrics,
    }
    if extra:
        out.update(dict(extra))
    return out
