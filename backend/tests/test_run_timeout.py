"""run 时长账本与真全局超时（方案 §4 P0-1）。

覆盖三类不变量：
1. 纯函数层：截止时间一次写入、暂停顺延有上限、active_ms 只累加执行段；
2. 账本层：分段执行（pause → resume）**deadline 不被重置**（Q-02 回归）；
3. 运行层：`_invoke_and_finalize` 喂给 wait_for 的是"剩余时间"而非满额预算；
   超时收尾写 partial 结果信封，**result 不再为 NULL**，且不新增事件类型。
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.engine import runtime as runtime_mod
from app.modules.engine import timing
from app.modules.engine.runtime import EngineRuntime
from app.modules.runs.models import Run

T0 = datetime(2026, 9, 17, 3, 0, 0, tzinfo=UTC)


# ---------- 1. 纯函数层 ----------


def test_resolve_timeout_prefers_run_snapshot() -> None:
    """run 创建时的 budget 快照优先，其次 Agent 配置，最后缺省 600s。"""
    assert timing.resolve_timeout_seconds({"timeout_seconds": 120}) == 120
    assert timing.resolve_timeout_seconds({"timeout_seconds": 120}, agent_timeout=30) == 120
    assert timing.resolve_timeout_seconds({}, agent_timeout=30) == 30
    assert timing.resolve_timeout_seconds({}) == timing.DEFAULT_TIMEOUT_SECONDS
    # 0 / None 视为未配置，不得被当成"永不超时"
    assert timing.resolve_timeout_seconds({"timeout_seconds": 0}) == timing.DEFAULT_TIMEOUT_SECONDS


def test_pause_extension_is_capped() -> None:
    """暂停顺延有上限：用户隔夜确认不能把超时保护变成无限期。"""
    assert timing.pause_extension(T0, T0 + timedelta(seconds=90), 3600) == 90.0
    assert timing.pause_extension(T0, T0 + timedelta(hours=5), 3600) == 3600.0
    assert timing.pause_extension(T0, T0 - timedelta(seconds=5), 3600) == 0.0


def test_accumulate_active_ms_ignores_waiting() -> None:
    """暂停/等待时长不进 active_ms；空闲段（无段起点）不重复累加。"""
    assert timing.accumulate_active_ms(0, T0, T0 + timedelta(seconds=3)) == 3000
    assert timing.accumulate_active_ms(3000, None, T0 + timedelta(seconds=99)) == 3000
    assert timing.accumulate_active_ms(None, T0, T0) == 0


def test_elapsed_ms_counts_whole_life() -> None:
    assert timing.elapsed_ms(T0, T0 + timedelta(seconds=10)) == 10_000
    assert timing.elapsed_ms(None, T0) == 0


def test_result_envelope_shape() -> None:
    """结果信封（P0-5 唯一写出点）：字段齐全、路径特有字段走 extra。"""
    metrics = timing.budget_metrics(
        {"iterations": 3, "tool_calls": 7}, elapsed_ms=1000, active_ms=800
    )
    envelope = timing.result_envelope(
        outcome="partial",
        text="部分结论",
        reason="timeout",
        metrics=metrics,
        extra={"partial": True, "deadline_at": (T0 + timedelta(seconds=2)).isoformat()},
    )
    assert envelope["schema"] == "run_result/v1"
    assert envelope["outcome"] == "partial"
    assert envelope["partial"] is True
    assert envelope["reason"] == "timeout"
    assert envelope["cards"] == []
    assert envelope["artifacts"] == []
    assert envelope["metrics"]["iterations"] == 3
    assert envelope["metrics"]["active_ms"] == 800
    assert timing.budget_metrics(None, elapsed_ms=0, active_ms=0)["tool_calls"] == 0


def test_coalesce_result_keeps_legacy_text_shape() -> None:
    """历史 `{"text": ...}` 读取侧补成 v0 信封，且不臆造 outcome。"""
    legacy = timing.coalesce_result({"text": "旧结果"})
    assert legacy is not None
    assert legacy["schema"] == "run_result/v0"
    assert legacy["outcome"] == "done"
    assert legacy["text"] == "旧结果"
    assert legacy["artifacts"] == []
    assert timing.coalesce_result(None) is None


# ---------- 2. 账本层：deadline 永不重置（Q-02 回归） ----------


def _new_run(timeout_seconds: int = 600) -> Run:
    return Run(
        id=uuid4(),
        status="pending",
        trigger="manual",
        input={},
        budget={"timeout_seconds": timeout_seconds},
        budget_used={},
        active_ms=0,
    )


def test_deadline_written_once_and_survives_segments() -> None:
    """首个执行段写 started_at/deadline_at；后续段只顺延暂停时长，不重取满额。"""
    rt = EngineRuntime()
    run = _new_run(timeout_seconds=600)

    rt._open_segment(run, "pending", T0)
    assert run.started_at == T0
    assert run.deadline_at == T0 + timedelta(seconds=600)
    assert run.active_ms == 0

    # 第 1 段执行 10s 后暂停
    rt._close_segment(run, T0 + timedelta(seconds=10))
    assert run.active_ms == 10_000
    run.paused_at = T0 + timedelta(seconds=10)

    # 暂停 90s 后恢复：deadline 顺延 90s，active 仍只算执行
    rt._open_segment(run, "paused_awaiting_confirm", T0 + timedelta(seconds=100))
    assert run.started_at == T0
    assert run.deadline_at == T0 + timedelta(seconds=690)
    assert run.paused_at is None

    # 第 2 段执行 20s：累计 30s，绝不出现"每段重新取 600s"
    rt._close_segment(run, T0 + timedelta(seconds=120))
    assert run.active_ms == 30_000


def test_pause_extension_capped_in_ledger() -> None:
    """暂停超过上限时只顺延上限部分——不会因长时间等待而无限保命。"""
    rt = EngineRuntime()
    run = _new_run(timeout_seconds=600)
    rt._open_segment(run, "pending", T0)
    run.paused_at = T0
    rt._open_segment(run, "paused_awaiting_confirm", T0 + timedelta(hours=5))
    # 顺延上限被 clamp 到 settings.max_run_pause_seconds 以内
    assert run.deadline_at is not None
    assert run.deadline_at <= T0 + timedelta(seconds=600 + 86_400)


def test_legacy_run_without_ledger_backfills_once() -> None:
    """存量 run（started_at 有、deadline_at 空）按 coalesce 语义补一次账本。"""
    rt = EngineRuntime()
    run = _new_run(timeout_seconds=600)
    run.started_at = T0
    rt._open_segment(run, "running", T0 + timedelta(seconds=30))
    assert run.deadline_at == T0 + timedelta(seconds=600)
    assert run.active_ms == 0  # 存量无法回溯，不臆造已完成时长


# ---------- 3. 运行层：wait_for 用剩余时间 + 超时保留结果 ----------


class _Snapshot:
    def __init__(self, messages: list) -> None:
        self.values = {"messages": messages}
        self.next: tuple = ()


class _FakeGraph:
    def __init__(self, *, sleep_seconds: float = 0.0, messages: list | None = None) -> None:
        self.sleep_seconds = sleep_seconds
        self.called = False
        self._messages = messages or []

    async def ainvoke(self, payload: object, config: object) -> dict:
        self.called = True
        await asyncio.sleep(self.sleep_seconds)
        return {"messages": self._messages}

    async def aget_state(self, config: object) -> _Snapshot:
        return _Snapshot(self._messages)


class _FakeDB:
    def __init__(self, run: Run) -> None:
        self.run = run

    async def __aenter__(self) -> "_FakeDB":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: object, pk: object) -> Run:
        return self.run

    async def commit(self) -> None: ...


def _install(monkeypatch: pytest.MonkeyPatch, rt: EngineRuntime, run: Run) -> None:
    """替换运行时的两个外部依赖：run 读取与 DB 会话（单测不依赖真库）。"""

    async def _load(run_id: str) -> Run:
        return run

    monkeypatch.setattr(rt, "_load_run", _load)
    monkeypatch.setattr(runtime_mod, "session_factory", lambda: _FakeDB(run))


def test_wait_for_uses_remaining_time_not_full_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q-02 回归：图挂起时按"剩余 2s"截断，而不是重新取满额 600s。"""
    rt = EngineRuntime()
    run = _new_run(timeout_seconds=600)
    run.status = "running"
    run.input = {"task_id": "t1"}
    run_id = str(run.id)
    run.started_at = datetime.now(UTC) - timedelta(seconds=598)
    run.deadline_at = datetime.now(UTC) + timedelta(seconds=2)
    _install(monkeypatch, rt, run)

    graph = _FakeGraph(sleep_seconds=30.0)
    rt.graph = graph  # type: ignore[assignment]
    timeouts: list[str] = []

    async def _record(run_id: str, *, phase: str, thread_id: str | None = None) -> None:
        timeouts.append(phase)

    monkeypatch.setattr(rt, "_finalize_timeout", _record)

    started = time.monotonic()
    asyncio.run(rt._invoke_and_finalize(run_id, "thread-1", "a1", {"messages": []}))
    elapsed = time.monotonic() - started

    assert graph.called
    assert timeouts == ["graph"]
    assert elapsed < 10.0, f"应按剩余 2s 截断，实际 {elapsed:.1f}s"


def test_exhausted_deadline_skips_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算已耗尽：不调用图，直接按超时收尾（避免再白跑一整段）。"""
    rt = EngineRuntime()
    run = _new_run(timeout_seconds=600)
    run.status = "running"
    run.input = {"task_id": "t1"}
    run_id = str(run.id)
    run.started_at = datetime.now(UTC) - timedelta(seconds=900)
    run.deadline_at = datetime.now(UTC) - timedelta(seconds=300)
    _install(monkeypatch, rt, run)

    graph = _FakeGraph()
    rt.graph = graph  # type: ignore[assignment]
    timeouts: list[str] = []

    async def _record(run_id: str, *, phase: str, thread_id: str | None = None) -> None:
        timeouts.append(phase)

    monkeypatch.setattr(rt, "_finalize_timeout", _record)
    asyncio.run(rt._invoke_and_finalize(run_id, "thread-1", "a1", {"messages": []}))

    assert not graph.called
    assert timeouts == ["pre_invoke"]


def test_finalize_timeout_persists_partial_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时也留结果：result 为 partial 信封、error 结构化，事件不扩散类型。"""
    rt = EngineRuntime()
    run = _new_run(timeout_seconds=2)
    run.status = "running"
    run_id = str(run.id)
    run.started_at = datetime.now(UTC) - timedelta(seconds=5)
    run.deadline_at = datetime.now(UTC)
    _install(monkeypatch, rt, run)

    class _Msg:
        type = "ai"
        content = "已完成的半截结论"

    rt.graph = _FakeGraph(messages=[_Msg()])  # type: ignore[assignment]
    events: list[tuple[str, dict]] = []
    finalized: list[tuple[str, str, dict]] = []

    async def _emit(run_id: str, event_type: str, payload: dict) -> int:
        events.append((event_type, payload))
        return len(events)

    async def _finalize(
        run_id: str,
        status: str,
        text: str,
        *,
        outcome: str | None = None,
        reason: str | None = None,
        extra: dict | None = None,
        cards: list | None = None,
        achieved: bool = True,
        error=None,
    ) -> None:
        finalized.append((status, text, {"outcome": outcome, "reason": reason, "extra": extra}))

    monkeypatch.setattr(rt, "emit_event", _emit)
    monkeypatch.setattr(rt, "_finalize", _finalize)

    asyncio.run(rt._finalize_timeout(run_id, phase="graph", thread_id="thread-1"))

    assert [e[0] for e in events] == ["error"]
    payload = events[0][1]
    assert payload["code"] == "timeout"
    assert payload["phase"] == "graph"
    assert payload["partial_result"] is True
    assert payload["active_ms"] >= 0
    # 账本在同一次提交里关闭执行段（暂停标记一并清掉）
    assert run.active_ms is not None and run.paused_at is None

    status, text, kwargs = finalized[0]
    assert status == "failed"  # 状态机不变：failed + error.code
    # 信封本身由 _finalize 组装（见 test_run_artifacts.py），此处只断言接线参数
    assert kwargs["outcome"] == "partial"
    assert kwargs["reason"] == "timeout"
    assert kwargs["extra"]["partial"] is True
    assert text == "已完成的半截结论"
