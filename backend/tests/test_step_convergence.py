"""P1-6 阻碍与决策的结构化可见：收敛原因枚举、前台收敛三动作、收敛事件。

覆盖：
1. 终态 → `STEP_BLOCK_REASONS` 的确定性映射（不再靠自由文本后缀判定）；
2. `reconcile_orphaned_awaits` 写入枚举 reason / 人话 note / 时间戳，并返回被收敛的支线；
3. `converge_blocked_step` 三动作的状态与 resolution 落库口径（尤其 requeue 不得顺手判完成）；
4. 收敛端点的事件留痕（close/requeue → `unblocked`，escalate → `blocked`）、
   无 run 归属支线只落状态不发事件、非法动作/非受阻支线的 422/409/404 映射；
5. runtime 终态收敛逐条补发 `blocked` 事件（引擎侧）与 reason 透传。

不依赖 DB：session 用最小替身，事件写入用记录替身。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app
from app.modules.auth.deps import get_current_user
from app.modules.engine.runtime import EngineRuntime, _block_reason_for
from app.modules.tasks import router as tasks_router
from app.modules.tasks import service as tasks_service
from app.modules.tasks.models import (
    STEP_BLOCK_REASONS,
    STEP_CONVERGE_ACTIONS,
    Task,
    TaskStep,
)
from app.modules.tasks.schemas import StepConvergeIn, TaskDetailOut

# ---------- 最小 session 替身 ----------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """按调用顺序回放预置结果：`get` / `scalars` 各一条队列。"""

    def __init__(
        self,
        *,
        get_rows: list[Any] | None = None,
        scalars_rows: list[list[Any]] | None = None,
        execute_rows: list[list[Any]] | None = None,
    ) -> None:
        self._get = list(get_rows or [])
        self._scalars = list(scalars_rows or [])
        self._execute = list(execute_rows or [])
        self.commits = 0
        self.rollbacks = 0
        self.refreshed: list[Any] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: Any, pk: Any) -> Any:
        return self._get.pop(0) if self._get else None

    async def scalars(self, stmt: Any) -> _Result:
        return _Result(self._scalars.pop(0) if self._scalars else [])

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        return _Result(self._execute.pop(0) if self._execute else [])

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)


def _step(**kw: Any) -> TaskStep:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "seq": 1,
        "name": "支线",
        "description": "",
        "kind": "branch",
        "status": "blocked",
        "source": "planner",
        "resolution": None,
    }
    base.update(kw)
    step = TaskStep(**base)
    return step


def _task(**kw: Any) -> Task:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "title": "主任务",
        "status": "active",
        "progress_done": 0,
        "progress_total": 0,
    }
    base.update(kw)
    return Task(**base)


# ---------- 1. 终态 → 受阻原因 ----------


def test_block_reason_mapping_is_deterministic() -> None:
    assert _block_reason_for("failed") == "run_ended"
    assert _block_reason_for("cancelled") == "user_cancelled"
    assert _block_reason_for("aborted") == "user_cancelled"
    assert _block_reason_for("timeout") == "deadline_exceeded"
    # 未知/非终态兜底 run_ended：宁可粗（有事因）也不能丢收敛
    assert _block_reason_for("done") == "run_ended"
    assert _block_reason_for("whatever") == "run_ended"


def test_block_reason_mapping_targets_enum_only() -> None:
    for status in ("failed", "cancelled", "aborted", "timeout", "done", "unknown"):
        assert _block_reason_for(status) in STEP_BLOCK_REASONS


# ---------- 2. reconcile_orphaned_awaits ----------


def test_reconcile_rejects_unknown_reason() -> None:
    db = _FakeSession()
    with pytest.raises(ValueError, match="非法的受阻原因"):
        asyncio.run(
            tasks_service.reconcile_orphaned_awaits(
                db, _task(), run_id=uuid.uuid4(), reason="因为下雨"
            )
        )


@pytest.mark.parametrize("reason", STEP_BLOCK_REASONS)
def test_reconcile_blocks_orphan_awaits_with_enum_reason(reason: str) -> None:
    """收敛原因落进枚举字段 + 人话 note，判定从此不看自由文本。"""
    task = _task()
    run_id = uuid.uuid4()
    step = _step(task_id=task.id, status="awaiting_user", run_id=run_id, resolution={"q": "选哪个"})
    db = _FakeSession(
        get_rows=[task],
        scalars_rows=[[step], [step]],
    )

    closed = asyncio.run(
        tasks_service.reconcile_orphaned_awaits(db, task, run_id=run_id, reason=reason)
    )

    assert [s.id for s in closed] == [step.id]
    assert step.status == "blocked"
    assert step.resolved_at is not None
    assert step.resolution is not None
    assert step.resolution["reason"] == reason
    assert isinstance(step.resolution["note"], str) and step.resolution["note"]
    assert "at" in step.resolution
    # 原有答复/问答字段不被覆盖（收敛只是补充阻碍信息）
    assert step.resolution["q"] == "选哪个"


# ---------- 3. converge_blocked_step 三动作 ----------


def test_converge_close_skips_step_and_closes_task() -> None:
    """关闭支线 = 不再需要：置 skipped，主线已收口时主任务正常收口。"""
    task = _task()
    main = _step(task_id=task.id, kind="main", status="done", seq=1)
    branch = _step(task_id=task.id, kind="branch", status="blocked", seq=2)
    db = _FakeSession(
        get_rows=[branch, task],
        scalars_rows=[[main, branch]],
    )

    step = asyncio.run(
        tasks_service.converge_blocked_step(
            db, task, branch.id, action="close", detail="客户说不用了"
        )
    )

    assert step is branch
    assert branch.status == "skipped"
    assert branch.resolution is not None
    assert branch.resolution["action"] == "close"
    assert branch.resolution["reason"] == "manual"
    assert branch.resolution["detail"] == "客户说不用了"
    assert "converged_at" in branch.resolution
    assert "escalated" not in branch.resolution
    # 主线全收口 + 无未完成支线 → 任务收口
    assert task.status == "done"
    assert task.progress_done == task.progress_total == 2


def test_converge_requeue_does_not_autoclose_task() -> None:
    """重新排队 ≠ 任务完成：回 pending 且不得触发 autoclose（否则看板谎报完成）。"""
    task = _task()
    main = _step(task_id=task.id, kind="main", status="done", seq=1)
    branch = _step(
        task_id=task.id, kind="branch", status="blocked", seq=2, resolution={"reason": "run_ended"}
    )
    branch.resolved_at = datetime.now(UTC)
    db = _FakeSession(
        get_rows=[branch, task],
        scalars_rows=[[main, branch]],
    )

    step = asyncio.run(tasks_service.converge_blocked_step(db, task, branch.id, action="requeue"))

    assert step is branch
    assert branch.status == "pending"
    assert branch.resolved_at is None, "重新排队必须清掉收敛时间，否则看板仍显示已收口"
    assert branch.resolution is not None
    assert branch.resolution["action"] == "requeue"
    assert task.status == "active", "重新排队不该顺手把主任务判完成"


def test_converge_escalate_keeps_blocked_and_marks_escalated() -> None:
    """转人工：仍受阻（进度不被算作完成），但标记已有人接手。"""
    task = _task()
    branch = _step(task_id=task.id, status="blocked")
    db = _FakeSession(
        get_rows=[branch, task],
        scalars_rows=[[branch]],
    )

    step = asyncio.run(tasks_service.converge_blocked_step(db, task, branch.id, action="escalate"))

    assert step is branch
    assert branch.status == "blocked"
    assert branch.resolution is not None
    assert branch.resolution["escalated"] is True
    assert branch.resolution["action"] == "escalate"
    assert task.status == "active"


def test_converge_rejects_unknown_action() -> None:
    db = _FakeSession(get_rows=[_step()])
    with pytest.raises(ValueError, match="非法的收敛动作"):
        asyncio.run(tasks_service.converge_blocked_step(db, _task(), uuid.uuid4(), action="重启"))


def test_converge_rejects_non_blocked_step() -> None:
    branch = _step(status="pending")
    db = _FakeSession(get_rows=[branch])
    with pytest.raises(ValueError, match="仅受阻"):
        asyncio.run(
            tasks_service.converge_blocked_step(
                db, _task(id=branch.task_id), branch.id, action="close"
            )
        )


def test_converge_returns_none_for_missing_or_foreign_step() -> None:
    """不存在 / 不属于本任务 → None（API 层 404），不抛错也不误改别的任务的支线。"""
    assert (
        asyncio.run(
            tasks_service.converge_blocked_step(
                _FakeSession(get_rows=[None]), _task(), uuid.uuid4(), action="close"
            )
        )
        is None
    )
    foreign = _step(task_id=uuid.uuid4())
    assert (
        asyncio.run(
            tasks_service.converge_blocked_step(
                _FakeSession(get_rows=[foreign]), _task(), foreign.id, action="close"
            )
        )
        is None
    )


def test_converge_without_detail_omits_field() -> None:
    """detail 只做人话补充：没填就不写空键（避免前端显示空行）。"""
    task = _task()
    branch = _step(task_id=task.id, status="blocked")
    db = _FakeSession(get_rows=[branch, task], scalars_rows=[[branch]])
    asyncio.run(tasks_service.converge_blocked_step(db, task, branch.id, action="escalate"))
    assert branch.resolution is not None and "detail" not in branch.resolution


# ---------- 4. 端点：事件留痕与错误映射 ----------


async def _detail_stub(_db: Any, task: Task) -> TaskDetailOut:
    now = datetime.now(UTC)
    return TaskDetailOut(
        id=task.id,
        worker_name="__common__",
        worker_display_name="通用任务",
        worker_version="",
        worker_icon=None,
        agent_id=uuid.uuid4(),
        title="主任务",
        status="active",
        progress_done=0,
        progress_total=0,
        progress_percent=0,
        out_of_scope_count=0,
        conversation=None,
        awaiting_confirm=False,
        active_run_id=None,
        started_at=None,
        finished_at=None,
        created_at=now,
        updated_at=now,
        steps=[],
        run_ids=[],
    )


class _Client:
    """TestClient + 依赖覆盖 + `_task_detail` 桩（本测试只关心收敛语义）。"""

    def __init__(self, db: _FakeSession, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
        app.dependency_overrides[get_db] = lambda: db
        monkeypatch.setattr(tasks_router, "_task_detail", _detail_stub)
        self.client = TestClient(app)

    def __enter__(self) -> TestClient:
        return self.client

    def __exit__(self, *exc: object) -> None:
        app.dependency_overrides.clear()


def _converge(client: TestClient, task_id: UUID, step_id: UUID, body: dict[str, Any]) -> Any:
    return client.post(f"/api/v1/tasks/{task_id}/steps/{step_id}/converge", json=body)


@pytest.mark.parametrize(
    ("action", "expected_event"),
    [("close", "unblocked"), ("requeue", "unblocked"), ("escalate", "blocked")],
)
def test_converge_endpoint_emits_event_on_owning_run(
    action: str, expected_event: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收敛动作必须留在该支线所属 run 的事件流里（可重放、可追溯）。"""
    emitted: list[tuple[Any, str, dict[str, Any]]] = []

    async def _record(run_id: Any, event_type: str, payload: dict[str, Any], *, db: Any) -> int:
        emitted.append((run_id, event_type, payload))
        return 1

    monkeypatch.setattr("app.modules.runs.events.emit_event", _record)

    run_id = uuid.uuid4()
    task = _task()
    branch = _step(task_id=task.id, status="blocked", run_id=run_id)
    db = _FakeSession(get_rows=[task, branch, task], scalars_rows=[[branch]])
    with _Client(db, monkeypatch) as client:
        resp = _converge(client, task.id, branch.id, {"action": action})

    assert resp.status_code == 200, resp.text
    assert len(emitted) == 1
    assert emitted[0][0] == run_id, "事件必须挂在支线所属 run 上"
    assert emitted[0][1] == expected_event
    payload = emitted[0][2]
    assert payload["step_id"] == str(branch.id)
    assert payload["task_id"] == str(task.id)
    assert payload["action"] == action
    assert payload["reason"] == "manual"
    assert payload["status"] == branch.status


def test_converge_endpoint_skips_event_without_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 run 归属的支线没有可附着的事件流：只落状态（规格里写明的边界）。"""
    emitted: list[Any] = []

    async def _record(*args: Any, **kwargs: Any) -> int:
        emitted.append(args)
        return 1

    monkeypatch.setattr("app.modules.runs.events.emit_event", _record)

    task = _task()
    orphan = _step(task_id=task.id, status="blocked", run_id=None)
    db = _FakeSession(get_rows=[task, orphan, task], scalars_rows=[[orphan]])
    with _Client(db, monkeypatch) as client:
        resp = _converge(client, task.id, orphan.id, {"action": "close"})

    assert resp.status_code == 200, resp.text
    assert emitted == []
    assert orphan.status == "skipped"


def test_converge_endpoint_status_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """404（任务/子任务不存在）、409（非受阻）= ValueError 的 HTTP 投影。"""
    task = _task()
    pending = _step(task_id=task.id, status="pending")

    with _Client(_FakeSession(get_rows=[None]), monkeypatch) as client:
        assert _converge(client, uuid.uuid4(), uuid.uuid4(), {"action": "close"}).status_code == 404

    with _Client(_FakeSession(get_rows=[task, None]), monkeypatch) as client:
        assert _converge(client, task.id, uuid.uuid4(), {"action": "close"}).status_code == 404

    db = _FakeSession(get_rows=[task, pending])
    with _Client(db, monkeypatch) as client:
        resp = _converge(client, task.id, pending.id, {"action": "close"})
    assert resp.status_code == 409
    assert db.rollbacks == 1


def test_converge_request_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """动作枚举与未知字段都挡在入口（422），避免脏动作进状态机。"""
    task = _task()
    step = _step(task_id=task.id)
    with _Client(_FakeSession(get_rows=[task, step]), monkeypatch) as client:
        assert _converge(client, task.id, step.id, {"action": "reopen"}).status_code == 422
        assert (
            _converge(client, task.id, step.id, {"action": "close", "reason": "x"}).status_code
            == 422
        )
        assert (
            _converge(
                client, task.id, step.id, {"action": "close", "detail": "x" * 501}
            ).status_code
            == 422
        )


def test_step_converge_schema_contract() -> None:
    assert STEP_CONVERGE_ACTIONS == ("close", "requeue", "escalate")
    assert StepConvergeIn(action="close").detail is None
    assert StepConvergeIn(action="escalate", detail="已找张工").detail == "已找张工"


def test_converge_route_is_registered() -> None:
    """契约锁：路径与方法（放在 PATCH .../steps/{id} 之后，靠完整路径区分）。"""
    paths = app.openapi()["paths"]
    path = "/api/v1/tasks/{task_id}/steps/{step_id}/converge"
    assert path in paths, sorted(p for p in paths if "converge" in p)
    assert "post" in paths[path]
    assert "200" in paths[path]["post"]["responses"]


# ---------- 5. runtime 终态收敛补发事件 ----------


def _runtime_with_run(
    monkeypatch: pytest.MonkeyPatch, task: Task
) -> tuple[EngineRuntime, list[tuple[Any, str, dict[str, Any]]]]:
    emitted: list[tuple[Any, str, dict[str, Any]]] = []

    async def _record(run_id: Any, event_type: str, payload: dict[str, Any], *, db: Any) -> int:
        emitted.append((run_id, event_type, payload))
        return 1

    monkeypatch.setattr("app.modules.runs.events.emit_event", _record)
    run_id = uuid.uuid4()
    run = SimpleNamespace(id=run_id, input={"task_id": str(task.id)}, conversation_id=None)
    runtime = EngineRuntime()
    monkeypatch.setattr(
        "app.modules.engine.runtime.session_factory",
        lambda: _FakeSession(get_rows=[run, task]),
    )
    return runtime, emitted


def test_runtime_reconcile_emits_blocked_per_converged_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每个被收敛的支线补发一条 blocked：阻碍因此在 SSE 与重放里都可见。"""
    task = _task()
    runtime, emitted = _runtime_with_run(monkeypatch, task)
    steps = [_step(task_id=task.id, status="awaiting_user"), _step(task_id=task.id)]

    async def _reconcile(db: Any, t: Task, *, run_id: UUID, reason: str) -> list[TaskStep]:
        assert reason == "deadline_exceeded", "runtime 必须把终态映射成枚举原因传下去"
        return steps

    monkeypatch.setattr(tasks_service, "reconcile_orphaned_awaits", _reconcile)

    asyncio.run(runtime._reconcile_awaiting_steps(str(uuid.uuid4()), _block_reason_for("timeout")))

    assert [e[1] for e in emitted] == ["blocked", "blocked"]
    assert {e[2]["step_id"] for e in emitted} == {str(s.id) for s in steps}
    assert {e[2]["task_id"] for e in emitted} == {str(task.id)}
    assert {e[2]["reason"] for e in emitted} == {"deadline_exceeded"}


def test_runtime_reconcile_silent_when_nothing_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task()
    runtime, emitted = _runtime_with_run(monkeypatch, task)

    async def _reconcile(db: Any, t: Task, *, run_id: UUID, reason: str) -> list[TaskStep]:
        return []

    monkeypatch.setattr(tasks_service, "reconcile_orphaned_awaits", _reconcile)

    asyncio.run(runtime._reconcile_awaiting_steps(str(uuid.uuid4()), "run_ended"))

    assert emitted == []
