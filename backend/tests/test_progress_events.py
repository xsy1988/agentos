"""P1-5 推送式进度：进度快照取数口径 + 平台 watcher 事件生成。

覆盖：
1. `progress` 登记进 `EVENT_TYPES`（四处同步之一，其余由注册表测试把关）；
2. `tasks_service.list_progress_snapshots` 的取数口径：行由**非终态 run** 驱动、
   进度读主任务反范式列、`label` 等待期间给阶段文案（不撒谎说"在做某步"）；
3. watcher 只在快照**变化**时发事件（巡检不得变成事件噪声）、无步骤骨架不发、
   进程结束后清内存记账；
4. 事件经 `runs/events.py:emit_event` 落库（唯一写入口 → 可经 SSE 实时推、可历史重放）；
5. watcher 生命周期：`start()` 真的拉起它、单轮异常不退出、`stop()` 收得掉。

不依赖 DB：session 用最小替身，事件写入用记录替身。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any
from uuid import UUID

import pytest

from app.core.config import settings
from app.modules.engine import runtime as runtime_mod
from app.modules.engine.backend import EVENT_TYPES
from app.modules.engine.runtime import EngineRuntime
from app.modules.runs.models import NON_TERMINAL_RUN_STATUSES, RUN_STATUSES
from app.modules.tasks import service as tasks_service
from tests.support.fake_db import FakeSession


def _snapshot(
    *,
    run_id: str | None = None,
    run_status: str = "running",
    done: int = 1,
    total: int = 3,
    label: str = "写报告",
) -> dict[str, Any]:
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "run_status": run_status,
        "done": done,
        "total": total,
        "label": label,
    }


def _runtime_with_events(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[EngineRuntime, list[tuple[Any, str, dict[str, Any]]]]:
    """runtime + 事件记录替身：拦截唯一写入口 `runs/events.py:emit_event`。"""
    emitted: list[tuple[Any, str, dict[str, Any]]] = []

    async def _record(run_id: Any, event_type: str, payload: dict[str, Any], *, db: Any) -> int:
        emitted.append((run_id, event_type, payload))
        return 1

    monkeypatch.setattr("app.modules.runs.events.emit_event", _record)
    return EngineRuntime(), emitted


# ---------- 1. 注册表 ----------


def test_progress_event_registered() -> None:
    assert "progress" in EVENT_TYPES


def test_non_terminal_run_statuses_cover_everything_not_terminal() -> None:
    """快照行集的口径：非终态状态集必须与 RUN_STATUSES 互补（漏一个就不推它的进度）。"""
    terminal = {s for s in RUN_STATUSES if s not in NON_TERMINAL_RUN_STATUSES}
    assert terminal == {"done", "failed", "cancelled"}
    assert "waiting_external" in NON_TERMINAL_RUN_STATUSES


# ---------- 2. 快照取数口径 ----------


def _run_task_row(
    *, conv_id: UUID, run_status: str, task_id: UUID, done: int, total: int
) -> tuple[Any, ...]:
    return (uuid.uuid4(), run_status, task_id, done, total)


def test_snapshot_only_scans_non_terminal_runs() -> None:
    db = FakeSession(execute_rows=[[]])
    asyncio.run(tasks_service.list_progress_snapshots(db))

    assert len(db.statements) == 1, "无行时不得再发第二次查询"
    in_lists = [
        v for v in db.statements[0].compile().params.values() if isinstance(v, (list, tuple, set))
    ]
    assert len(in_lists) == 1, "行集应由 run.status IN (...) 过滤"
    scanned = list(in_lists[0])
    assert sorted(scanned) == sorted(NON_TERMINAL_RUN_STATUSES)
    assert not {"done", "failed", "cancelled"} & set(scanned), "终态 run 不再推进度"


def test_snapshot_prefers_phase_label_over_active_step() -> None:
    """等待外部回调时 label 讲"在等什么"——否则用户会以为模型还在干活。"""
    waiting_task = uuid.uuid4()
    running_task = uuid.uuid4()
    rows = [
        _run_task_row(
            conv_id=uuid.uuid4(), run_status="running", task_id=running_task, done=1, total=3
        ),
        _run_task_row(
            conv_id=uuid.uuid4(),
            run_status="waiting_external",
            task_id=waiting_task,
            done=1,
            total=3,
        ),
    ]
    steps = [(waiting_task, "等采购单"), (running_task, "写报告")]
    db = FakeSession(execute_rows=[rows, steps])

    snaps = asyncio.run(tasks_service.list_progress_snapshots(db))

    assert {(s["run_status"], s["done"], s["total"]) for s in snaps} == {
        ("running", 1, 3),
        ("waiting_external", 1, 3),
    }
    assert sorted(s["label"] for s in snaps) == ["写报告", "等待外部回调"]


@pytest.mark.parametrize(
    ("run_status", "label"),
    [("pending", "排队中"), ("paused_awaiting_confirm", "等待确认")],
)
def test_snapshot_phase_labels_are_explicit(run_status: str, label: str) -> None:
    task_id = uuid.uuid4()
    db = FakeSession(
        execute_rows=[
            [
                _run_task_row(
                    conv_id=uuid.uuid4(), run_status=run_status, task_id=task_id, done=0, total=2
                )
            ],
            [],
        ]
    )

    snaps = asyncio.run(tasks_service.list_progress_snapshots(db))

    assert snaps[0]["label"] == label


def test_snapshot_label_blank_without_active_step() -> None:
    """没有活跃子任务时不编造文案（前端据此只显示数字）。"""
    task_id = uuid.uuid4()
    db = FakeSession(
        execute_rows=[
            [
                _run_task_row(
                    conv_id=uuid.uuid4(), run_status="running", task_id=task_id, done=2, total=2
                )
            ],
            [],
        ]
    )

    snaps = asyncio.run(tasks_service.list_progress_snapshots(db))

    assert snaps[0]["label"] == ""


def test_snapshot_active_step_picks_lowest_seq() -> None:
    """同一任务多条活跃步骤时取 seq 最小的（与任务卡"当前步骤"同口径）。"""
    task_id = uuid.uuid4()
    db = FakeSession(
        execute_rows=[
            [
                _run_task_row(
                    conv_id=uuid.uuid4(), run_status="running", task_id=task_id, done=0, total=4
                )
            ],
            [(task_id, "第一步"), (task_id, "第二步")],
        ]
    )

    snaps = asyncio.run(tasks_service.list_progress_snapshots(db))

    assert snaps[0]["label"] == "第一步"
    in_lists = [
        v for v in db.statements[1].compile().params.values() if isinstance(v, (list, tuple, set))
    ]
    active_statuses = [v for v in in_lists if set(v) <= {"doing", "awaiting_user", "blocked"}]
    assert active_statuses, "活跃步骤须含 doing/awaiting_user/blocked——否则等待期间 label 会空掉"


# ---------- 3. watcher：变化才发 ----------


def _patch_snapshots(
    monkeypatch: pytest.MonkeyPatch, batches: list[list[dict[str, Any]]]
) -> list[int]:
    calls: list[int] = []

    async def _snap(_db: Any) -> list[dict[str, Any]]:
        calls.append(len(calls))
        return batches[min(len(calls) - 1, len(batches) - 1)]

    monkeypatch.setattr(tasks_service, "list_progress_snapshots", _snap)
    monkeypatch.setattr(runtime_mod, "session_factory", lambda: FakeSession())
    return calls


def test_sweep_emits_on_first_sight_then_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """首见发一份快照（前端不必等模型），此后同值不再发（巡检不是噪声源）。"""
    runtime, emitted = _runtime_with_events(monkeypatch)
    snap = _snapshot()
    _patch_snapshots(monkeypatch, [[snap]])

    asyncio.run(runtime._sweep_progress())
    assert [e[1] for e in emitted] == ["progress"]
    assert emitted[0][0] == snap["run_id"]
    assert emitted[0][2] == {"done": 1, "total": 3, "label": "写报告"}
    assert set(emitted[0][2]) == {"done", "total", "label"}, "payload 形状是前端契约"

    emitted.clear()
    asyncio.run(runtime._sweep_progress())
    assert emitted == []


def test_sweep_emits_when_progress_advances_without_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前台收敛支线/等待回传推进进度：模型不在场也必须推出去（P1-5 的核心诉求）。"""
    runtime, emitted = _runtime_with_events(monkeypatch)
    run_id = str(uuid.uuid4())
    _patch_snapshots(
        monkeypatch,
        [
            [_snapshot(run_id=run_id, done=1, total=3)],
            [_snapshot(run_id=run_id, done=2, total=3)],
            [_snapshot(run_id=run_id, done=2, total=3)],
        ],
    )

    asyncio.run(runtime._sweep_progress())
    asyncio.run(runtime._sweep_progress())
    asyncio.run(runtime._sweep_progress())

    assert [e[2]["done"] for e in emitted] == [1, 2]


def test_sweep_emits_when_run_enters_and_leaves_external_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """与 P0-4 联动：进入/离开等待各推一份快照，等待期间进度由平台（非模型）表达。"""
    runtime, emitted = _runtime_with_events(monkeypatch)
    run_id = str(uuid.uuid4())
    _patch_snapshots(
        monkeypatch,
        [
            [_snapshot(run_id=run_id, run_status="running", label="写报告")],
            [_snapshot(run_id=run_id, run_status="waiting_external", label="等待外部回调")],
            [_snapshot(run_id=run_id, run_status="running", label="写报告")],
        ],
    )

    for _ in range(3):
        asyncio.run(runtime._sweep_progress())

    assert [e[2]["label"] for e in emitted] == ["写报告", "等待外部回调", "写报告"]


def test_sweep_skips_runs_without_step_skeleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """通用会话/工具型 run 无步骤骨架：没有可表达的进度，不发首帧噪声。"""
    runtime, emitted = _runtime_with_events(monkeypatch)
    _patch_snapshots(monkeypatch, [[_snapshot(total=0, done=0, label="")]])

    asyncio.run(runtime._sweep_progress())

    assert emitted == []
    assert len(runtime._progress_seen) == 1, "仍要记账，否则每轮都被当成变化"


def test_sweep_forgets_terminal_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """run 收口后清掉内存记账：watcher 的内存不随历史 run 无限增长。"""
    runtime, emitted = _runtime_with_events(monkeypatch)
    snap = _snapshot()
    _patch_snapshots(monkeypatch, [[snap], []])

    asyncio.run(runtime._sweep_progress())
    assert snap["run_id"] in runtime._progress_seen
    asyncio.run(runtime._sweep_progress())
    assert runtime._progress_seen == {}


def test_sweep_event_goes_through_single_write_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """事件必须落在 `run_events`（唯一写入口）→ 可 SSE 实时推、可历史重放。"""
    runtime, emitted = _runtime_with_events(monkeypatch)
    _patch_snapshots(monkeypatch, [[_snapshot()]])

    asyncio.run(runtime._sweep_progress())

    assert len(emitted) == 1, "emit_event 是 run_events 的唯一写入口，进度事件只能经它落库"


# ---------- 4. watcher 生命周期 ----------


def test_progress_watcher_survives_iteration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """单轮异常不得让 watcher 退出（退出即进度永久停在原地）。"""
    runtime, emitted = _runtime_with_events(monkeypatch)
    batches = [[_snapshot()]]
    calls = _patch_snapshots(monkeypatch, batches)
    boom = {"n": 0}
    inner = tasks_service.list_progress_snapshots

    async def _flaky(db: Any) -> list[dict[str, Any]]:
        if boom["n"] == 0:
            boom["n"] = 1
            raise RuntimeError("一轮巡检失败")
        return await inner(db)

    monkeypatch.setattr(tasks_service, "list_progress_snapshots", _flaky)
    monkeypatch.setattr(settings, "progress_watch_interval_seconds", 0)

    async def _run() -> None:
        task = asyncio.create_task(runtime._progress_watcher())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    assert boom["n"] == 1 and len(calls) > 1, "异常后必须继续巡检"
    assert [e[1] for e in emitted] == ["progress"]


def test_start_launches_progress_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """`start()` 必须真的拉起 watcher——没拉起等于 P1-5 静默失效。"""
    spawned: list[str | None] = []

    class _StubSaver:
        async def setup(self) -> None: ...

    class _StubSaverCM:
        async def __aenter__(self) -> _StubSaver:
            return _StubSaver()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    def _spy_create_task(coro: Any, name: str | None = None) -> None:
        spawned.append(name)
        coro.close()  # 只验装配，不真跑（真跑会连库/建图）

    monkeypatch.setattr(
        runtime_mod.AsyncPostgresSaver,
        "from_conn_string",
        staticmethod(lambda _dsn: _StubSaverCM()),
    )
    monkeypatch.setattr("app.modules.engine.hooks_impl.build_default_chain", lambda _emit: None)
    monkeypatch.setattr(runtime_mod, "build_graph", lambda _rt: None)
    monkeypatch.setattr(runtime_mod.asyncio, "create_task", _spy_create_task)
    runtime = EngineRuntime()

    async def _noop() -> None: ...

    monkeypatch.setattr(runtime, "_reconcile_orphans", _noop)
    monkeypatch.setattr(runtime, "_listen_inbox", _noop)

    asyncio.run(runtime.start())

    assert "engine-progress-watcher" in spawned


def test_stop_cancels_progress_watcher() -> None:
    runtime = EngineRuntime()

    async def _run() -> None:
        runtime._progress_watcher_task = asyncio.create_task(runtime._progress_watcher())
        await asyncio.sleep(0)
        await runtime.stop()
        assert runtime._progress_watcher_task.cancelled()

    asyncio.run(_run())
