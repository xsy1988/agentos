"""run 级幂等键（方案 §5 P1-9）。

缺陷：网络抖动/用户连点会让同一条消息提交两次——两次都落消息、都建 run、
都投 inbox，用户看到两个并行的 run 与重复的计费。

修后的规则：

1. 键存在 `runs.input.client_message_id`，唯一性由**部分唯一表达式索引**
   `(conversation_id, input ->> 'client_message_id') WHERE input ? 'client_message_id'`
   保证（不带键的 run，如定时器/回调续跑，不受约束）；
2. 同会话同键的重复提交**不落消息、不建 run、不投 inbox**，直接返回首次那个 run；
3. 两个入口（`POST /conversations/{id}/messages` 与 `POST /tasks`）共用一个键；
   `POST /tasks` 的判重必须早于建会话/任务，否则连点会多出空任务；
4. 并发穿透查重时靠唯一索引拦下，**整单回滚**后返回胜出的 run（不留"有消息无 run"）。
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.modules.conversations import service as conv_service
from app.modules.conversations.models import Message
from app.modules.conversations.schemas import MessageIn
from app.modules.tasks import router as tasks_router
from app.modules.tasks.schemas import TaskCreateIn, TaskOut

# ---------- 假 DB ----------


class _Scalars:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def first(self) -> Any:
        return self._items[0] if self._items else None


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalars(self) -> _Scalars:
        return _Scalars([self._row] if self._row is not None else [])


class _FakeDB:
    """按调用形态分流：无 params = 查重 select（吃 `lookups` 队列），有 params = 写。"""

    def __init__(self, lookups: list[Any] = (), get_result: Any = None) -> None:
        self.lookups = list(lookups)
        self.get_result = get_result
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.added: list[Any] = []
        self.flushes = 0
        self.fail_at_flush: int | None = None
        self.flush_error: Exception | None = None
        self.rolled_back = False

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        if params is None:
            row = self.lookups.pop(0) if self.lookups else None
            return _Result(row)
        self.statements.append((str(statement), params))
        return _Result(None)

    async def get(self, model: Any, pk: Any) -> Any:
        return self.get_result

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        if self.flush_error is not None and self.flushes == self.fail_at_flush:
            raise self.flush_error

    async def rollback(self) -> None:
        self.rolled_back = True


def _conv(state: str = "new") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        title="新会话",
        message_count=0,
        last_message_at=None,
        status=state,
    )


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        tool_budget=20, max_iterations=8, max_tokens_per_run=None, timeout_seconds=300
    )


def _run(**over: Any) -> SimpleNamespace:
    base = {
        "id": uuid4(),
        "conversation_id": uuid4(),
        "input": {"text": "hi", "client_message_id": "k1"},
        "status": "pending",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _send(db: _FakeDB, conv: SimpleNamespace, **kw: Any) -> Any:
    async def _go() -> Any:
        return await conv_service.create_user_run(
            db, conv, _agent(), text="你好", attachments=[], **kw
        )

    return asyncio.run(_go())


# ---------- 1. 键的归一化 ----------


def test_normalize_treats_blank_as_absent() -> None:
    """前端传空串/全空白不算键：否则所有空键提交会互相判重。"""
    assert conv_service.normalize_client_message_id(None) is None
    assert conv_service.normalize_client_message_id("") is None
    assert conv_service.normalize_client_message_id("   \t\n") is None
    assert conv_service.normalize_client_message_id(" abc ") == "abc"


def test_normalize_truncates_to_column_budget() -> None:
    """超长键按上限截断（与 `MessageIn` 的 max_length 同口径）。"""
    long_key = "x" * 300
    out = conv_service.normalize_client_message_id(long_key)
    assert out == "x" * conv_service.CLIENT_MESSAGE_ID_MAX


def test_schemas_cap_client_message_id_length() -> None:
    """入口就挡住超长键：避免"截断后相等"的隐性判重。"""
    ok = "y" * conv_service.CLIENT_MESSAGE_ID_MAX
    assert MessageIn(text="hi", client_message_id=ok).client_message_id == ok
    assert TaskCreateIn(worker_name="w", client_message_id=ok).client_message_id == ok
    assert MessageIn(text="hi").client_message_id is None

    with pytest.raises(ValidationError):
        MessageIn(text="hi", client_message_id=ok + "z")
    with pytest.raises(ValidationError):
        TaskCreateIn(worker_name="w", client_message_id=ok + "z")


# ---------- 2. 查重口径 ----------


def test_lookup_scopes_to_conversation_when_given() -> None:
    """给了会话就限定在会话内（与唯一索引同口径），不给则全局找。"""
    conv_id = uuid4()

    assert asyncio.run(conv_service.find_run_by_client_message_id(_FakeDB(), "k1", conv_id)) is None
    global_stmt = _capture_lookup("k1", None)
    scoped_stmt = _capture_lookup("k1", conv_id)

    global_where = str(global_stmt.whereclause)
    scoped_where = str(scoped_stmt.whereclause)
    assert "conversation_id" not in global_where and "conversation_id" in scoped_where
    # 表达式与唯一索引逐字同形（字面量键名，否则吃不到索引）：JSONB 取键
    assert "input ->> 'client_message_id'" in global_where
    # 键名走字面量（吃得到索引），只有键值本身是绑定参数
    global_params = set(global_stmt.compile().params.values())
    assert "k1" in global_params and conv_id not in global_params
    assert conv_id in set(scoped_stmt.compile().params.values())


def _capture_lookup(key: str, conv_id: UUID | None) -> Any:
    """跑一次查重，把发出去的 select 捞回来断言（只看 where 子句）。"""
    captured: dict[str, Any] = {}

    class _Capturing(_FakeDB):
        async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            captured["stmt"] = statement
            return await super().execute(statement, params)

    asyncio.run(conv_service.find_run_by_client_message_id(_Capturing(), key, conv_id))
    return captured["stmt"]


# ---------- 3. 重复提交：不落消息、不再投 inbox ----------


def test_duplicate_submit_returns_first_run_untouched() -> None:
    """同会话同键第二次提交 → 原 run 原样返回，消息数/inbox 事件都不再增加。"""
    conv = _conv()
    existing = _run(conversation_id=conv.id)
    db = _FakeDB(lookups=[existing])

    out = _send(db, conv, client_message_id="k1")

    assert out is existing
    assert db.added == []
    assert db.statements == []
    assert conv.message_count == 0


def test_first_submit_snapshots_key_into_run_input() -> None:
    """首次提交：键随 input 快照落库（唯一索引建在这个表达式上），消息与 inbox 照旧。"""
    conv = _conv()
    db = _FakeDB(lookups=[None])

    run = _send(db, conv, client_message_id=" k1 ")

    assert run.input["client_message_id"] == "k1"  # 落库前已归一化
    assert isinstance(db.added[0], Message)
    assert conv.message_count == 1
    sql = " ".join(s for s, _ in db.statements)
    assert "INSERT INTO inbox_events" in sql and "pg_notify" in sql


def test_absent_key_keeps_run_input_clean() -> None:
    """不带键的提交（旧客户端/定时器）行为不变：input 里不出现该字段。"""
    conv = _conv()
    db = _FakeDB(lookups=[None])

    run = _send(db, conv)

    assert "client_message_id" not in run.input
    assert conv.message_count == 1


# ---------- 4. 竞态：唯一索引拦下后整单回滚 ----------


def test_race_rolls_back_and_returns_winner() -> None:
    """两次并发提交穿过查重 → 唯一索引只放行一个；落败方整单回滚并返回胜者。

    回滚（而不是回滚 savepoint）是必须的：本请求的 message 已 flush 过，
    只回滚 run 会留下"有消息无 run"的脏数据。返回的也必须是重查到的实例——
    原实例在 rollback 后已失效。
    """
    conv = _conv()
    winner = _run(conversation_id=conv.id)
    db = _FakeDB(lookups=[None, winner])
    db.flush_error = IntegrityError("INSERT INTO runs", {}, Exception("uq_runs_client_message_id"))
    db.fail_at_flush = 2  # 1 = message，2 = run

    out = _send(db, conv, client_message_id="k1")

    assert db.rolled_back
    assert out is winner
    assert db.statements == []


def test_unkeyed_integrity_error_is_not_swallowed() -> None:
    """没带键时唯一索引不该报错；真报了（其它约束）必须原样抛出，不能猜。"""
    conv = _conv()
    db = _FakeDB(lookups=[None])
    db.flush_error = IntegrityError("INSERT INTO runs", {}, Exception("other_constraint"))
    db.fail_at_flush = 2

    with pytest.raises(IntegrityError):
        _send(db, conv)


# ---------- 5. POST /tasks：判重必须早于建会话/任务 ----------


def _task_out(task: SimpleNamespace) -> TaskOut:
    now = datetime.now(UTC)
    return TaskOut(
        id=task.id,
        worker_name="weather",
        worker_display_name="天气",
        worker_version="1.0.0",
        worker_icon=None,
        agent_id=uuid4(),
        title="天气",
        status="active",
        progress_done=0,
        progress_total=1,
        progress_percent=0,
        out_of_scope_count=0,
        conversation=None,
        awaiting_confirm=False,
        active_run_id=None,
        started_at=None,
        finished_at=None,
        created_at=now,
        updated_at=now,
    )


def _patch_task_deps(monkeypatch: pytest.MonkeyPatch, db: _FakeDB, existing: Any) -> None:
    from app.modules.tasks import service as tasks_service

    monkeypatch.setattr(
        tasks_router, "registry", SimpleNamespace(get_meta=lambda _n: SimpleNamespace(enabled=True))
    )

    async def _resolve_agent(_db: Any, _agent_id: Any) -> Any:
        return _agent()

    async def _resolve_model(_db: Any, _provider_id: Any) -> None:
        return None

    async def _lookup(_db: Any, key: str, conv_id: UUID | None = None) -> Any:
        return existing

    async def _decorate(_db: Any, tasks: list[Any]) -> list[Any]:
        return [_task_out(t) for t in tasks]

    monkeypatch.setattr(conv_service, "resolve_agent", _resolve_agent)
    monkeypatch.setattr(conv_service, "resolve_model_override", _resolve_model)

    async def _must_not_create(_db: Any, *a: Any, **kw: Any) -> Any:
        raise AssertionError("判重命中后不应再建任务骨架")

    monkeypatch.setattr(conv_service, "find_run_by_client_message_id", _lookup)
    monkeypatch.setattr(tasks_router, "_decorate_tasks", _decorate)
    monkeypatch.setattr(tasks_service, "create_task", _must_not_create)


def _post_task(db: _FakeDB, **kw: Any) -> Any:
    body = TaskCreateIn(worker_name="weather", text="今天天气", **kw)

    async def _go() -> Any:
        return await tasks_router.create_task(body, db=db)

    return asyncio.run(_go())


def test_create_task_duplicate_returns_existing_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连点「新开会话并发送」：返回既有 任务/会话/run，且不再建会话与任务骨架。"""
    task = SimpleNamespace(id=uuid4(), conversation_id=uuid4(), worker_name="weather")
    existing = _run(
        conversation_id=task.conversation_id,
        input={"text": "hi", "client_message_id": "k1", "task_id": str(task.id)},
    )
    db = _FakeDB(get_result=task)
    _patch_task_deps(monkeypatch, db, existing)

    out = _post_task(db, client_message_id="k1")

    assert db.added == []  # 关键：没有多出来的空会话 / 空主任务
    assert out.task.id == task.id
    assert out.conversation_id == existing.conversation_id
    assert out.run_id == existing.id


def test_create_task_duplicate_without_task_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """键命中的 run 已无法解析出任务实例（数据被清理）→ 409 而不是半截返回。"""
    existing = _run(input={"text": "hi", "client_message_id": "k1", "task_id": str(uuid4())})
    db = _FakeDB(get_result=None)
    _patch_task_deps(monkeypatch, db, existing)

    with pytest.raises(HTTPException) as exc:
        _post_task(db, client_message_id="k1")

    assert exc.value.status_code == 409
    assert db.added == []
