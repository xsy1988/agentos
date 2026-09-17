"""有界并发与准入控制（方案 §5 P1-1）。

缺陷：`_spawn_run_task` 直接 `asyncio.create_task`，无任何上限——并发不可控、
不可观测。修后的规则：

1. 只闸**新 run**（inbox `user_input`）：超容即拒，**不静默排队**；
2. 超容时给可见反馈（`run_status` 事件 + `run.input.admitted=false`），run 行保持
   pending 供用户重发；
3. 续跑（`confirmation` / `resume`）**不设闸**：丢弃续跑会留下永远悬停的等待行，
   比短暂超并发更糟（P0-4 的"回调必被处理"优先级更高）；
4. 活跃 run 数可观测：`GET /api/v1/runs/capacity`。
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.modules.auth.deps import get_current_user
from app.modules.engine import runtime as runtime_mod
from app.modules.engine.runtime import EngineRuntime

# ---------- 假 DB（run 行 + inbox 行） ----------


class _FakeSession:
    def __init__(self, statements: list[tuple[str, dict[str, Any]]], run: Any) -> None:
        self._statements = statements
        self._run = run

    async def get(self, model: Any, pk: Any) -> Any:
        return self._run

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        self._statements.append((str(statement), params or {}))

    async def commit(self) -> None: ...

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None: ...


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    statements: list[tuple[str, dict[str, Any]]] = []
    run = SimpleNamespace(status="pending", input={"conversation_id": str(uuid4())})
    monkeypatch.setattr(
        runtime_mod, "session_factory", lambda: _FakeSession(statements, run)
    )
    return statements, run


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    sink: list[tuple[str, str, dict[str, Any]]] = []

    async def _emit(
        _self: EngineRuntime, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> int:
        sink.append((run_id, event_type, payload))
        return len(sink)

    monkeypatch.setattr(EngineRuntime, "emit_event", _emit)
    return sink


def _busy(runtime: EngineRuntime, n: int) -> None:
    """占位活跃任务：准入只看 `_run_tasks` 的条目数。"""
    for _ in range(n):
        runtime._run_tasks[str(uuid4())] = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]


# ---------- 1. 容量判定 ----------


def test_capacity_gate_uses_configurable_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限可配（默认 4，可被 settings 覆盖），活跃数即 `_run_tasks` 条目数。"""
    monkeypatch.setattr(settings, "max_concurrent_runs", 2)
    runtime = EngineRuntime()

    assert runtime.active_run_count() == 0 and runtime._has_capacity()

    _busy(runtime, 1)
    assert runtime.active_run_count() == 1 and runtime._has_capacity()

    _busy(runtime, 1)  # 到达上限
    assert runtime.active_run_count() == 2
    assert not runtime._has_capacity()

    monkeypatch.setattr(settings, "max_concurrent_runs", 3)
    assert runtime._has_capacity()  # 上限提高即恢复准入


# ---------- 2. 拒绝路径：反馈 + 留痕 ----------


def test_reject_records_admission_and_emits_visible_feedback(
    monkeypatch: pytest.MonkeyPatch,
    fake_db: tuple[list[tuple[str, dict[str, Any]]], Any],
    events: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """超容：run.input 落 `admitted=false` + 原因，inbox 行记 failed，事件带人话说明。"""
    monkeypatch.setattr(settings, "max_concurrent_runs", 1)
    statements, run = fake_db
    runtime = EngineRuntime()
    _busy(runtime, 1)
    run_id = str(uuid4())

    asyncio.run(runtime._reject_over_capacity(run_id, {"id": 42}))

    assert run.input["admitted"] is False
    assert run.input["admission"]["reason"] == "capacity_exceeded"
    assert run.input["admission"]["active_runs"] == 1
    assert run.input["admission"]["max_concurrent_runs"] == 1
    assert run.input["conversation_id"]  # 既有 input 字段不丢

    inbox_writes = [p for s, p in statements if "inbox_events" in s]
    assert inbox_writes == [{"id": 42}]

    assert len(events) == 1
    _, event_type, payload = events[0]
    assert event_type == "run_status"
    assert payload["status"] == "pending"  # run 未被推进，也没被标失败
    assert payload["admitted"] is False and payload["reason"] == "capacity_exceeded"
    assert "并发上限 1" in payload["detail"]


# ---------- 3. 派发路径：新 run 受闸，续跑不受闸 ----------


def _handle(event_type: str, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": 7, "event_type": event_type, "target_run_id": run_id, "payload": payload}


def test_user_input_over_capacity_is_rejected_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超容时新 run 不进执行队列：不创建 task，走拒绝分支给反馈。"""
    monkeypatch.setattr(settings, "max_concurrent_runs", 1)
    runtime = EngineRuntime()
    _busy(runtime, 1)
    run_id = str(uuid4())
    rejected: list[str] = []

    def _process_run(self: EngineRuntime, rid: str) -> Any:
        async def _never() -> None: ...

        return _never()

    async def _reject(self: EngineRuntime, rid: str, event: dict[str, Any]) -> None:
        rejected.append(rid)

    monkeypatch.setattr(EngineRuntime, "_process_run", _process_run)
    monkeypatch.setattr(EngineRuntime, "_reject_over_capacity", _reject)

    asyncio.run(runtime._handle(_handle("user_input", run_id, {"run_id": run_id})))

    assert rejected == [run_id]
    assert run_id not in runtime._run_tasks
    assert runtime.active_run_count() == 1  # 拒绝不改动既有活跃数


def test_user_input_within_capacity_is_spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_concurrent_runs", 4)
    runtime = EngineRuntime()
    run_id = str(uuid4())
    started: list[str] = []

    def _process_run(self: EngineRuntime, rid: str) -> Any:
        async def _work() -> None:
            started.append(rid)
            await asyncio.sleep(0)

        return _work()

    monkeypatch.setattr(EngineRuntime, "_process_run", _process_run)

    async def _scenario() -> None:
        await runtime._handle(_handle("user_input", run_id, {"run_id": run_id}))
        assert runtime.active_run_count() == 1  # 已占一个名额
        tasks = list(runtime._run_tasks.values())
        await asyncio.gather(*tasks)

    asyncio.run(_scenario())

    assert started == [run_id]
    assert runtime.active_run_count() == 0  # 结束后归还名额
    assert run_id not in runtime._run_tasks


def test_resume_is_never_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """续跑不受闸：即使已超容，唤醒也必须被派发（否则等待行永远悬停）。"""
    monkeypatch.setattr(settings, "max_concurrent_runs", 1)
    runtime = EngineRuntime()
    _busy(runtime, 3)  # 已超容
    run_id = str(uuid4())

    def _resume_run(self: EngineRuntime, rid: str, value: Any) -> Any:
        async def _work() -> None:
            await asyncio.sleep(0)

        return _work()

    monkeypatch.setattr(EngineRuntime, "_resume_run", _resume_run)

    async def _scenario() -> None:
        await runtime._handle(_handle("resume", run_id, {"kind": "await", "status": "resolved"}))
        tasks = list(runtime._run_tasks.values())[3:]
        assert tasks, "续跑必须被派发"
        await asyncio.gather(*tasks)

    asyncio.run(_scenario())


# ---------- 4. 观测面 ----------


def test_capacity_endpoint_reports_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /runs/capacity` 返回活跃数 / 上限 / 是否还收新活；字面路由不被 /{run_id} 吞。"""
    monkeypatch.setattr(settings, "max_concurrent_runs", 2)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    try:
        client = TestClient(app)
        body = client.get("/api/v1/runs/capacity").json()
    finally:
        app.dependency_overrides.clear()

    assert body == {"active_runs": 0, "max_concurrent_runs": 2, "admission_open": True}
