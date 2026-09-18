"""测试共用的假 DB 替身（最小 AsyncSession 实现）。

这些替身原先散在多个测试文件里各写一份，收敛到这里统一维护。构造全部是**同步**的
普通对象，测试继续按仓库约定用 `asyncio.run(...)` 驱动被测协程 —— 不引入 pytest-asyncio。

三种形态：
- `FakeSession`：队列回放型，`get` / `scalar` / `scalars` / `execute` 各有预置队列，
  适合服务层与运行时用例；
- `RoutingSession`：按语句路由的只读型，`router(stmt)` 决定返回哪些行，适合
  "读几处、每处口径不同" 的纯查询用例（输入预检、取件通道等）；
- `RecordingSession`：只记账的写路径最小替身，`get` 固定返回预置行，适合
  "抓 SQL 形态 + 固定读取" 的用例（hook、准入、终态落库等）。
"""

from collections.abc import Callable
from typing import Any


class FakeResult:
    """假 `Result`：只需要 `.all()` 与可迭代（并兼容 `.scalars()` 链式写法）。"""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = list(rows or [])

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


class FakeSession:
    """队列回放型会话替身：按调用顺序消费预置结果，并记录所有下发语句。

    记账字段：`added`（`add` 的对象）、`commits` / `rollbacks`、`refreshed`、
    `statements`（`scalar` / `scalars` / `execute` 下发的全部语句，原始对象）、
    `executed`（`execute` 的 `(stmt, params)`）、`scalars_calls`、`flushes`。

    `commit_error` / `fail_on` / `on_refresh` 是给故障与副作用用例留的注入口。
    """

    def __init__(
        self,
        *,
        get_rows: list[Any] | None = None,
        scalar_rows: list[Any] | None = None,
        scalars_rows: list[list[Any]] | None = None,
        execute_rows: list[list[Any]] | None = None,
        commit_error: Exception | None = None,
        fail_on: str | None = None,
        on_refresh: Any = None,
    ) -> None:
        self._get = list(get_rows or [])
        self._scalar = list(scalar_rows or [])
        self._scalars = list(scalars_rows or [])
        self._execute = list(execute_rows or [])
        self.commit_error = commit_error
        self.fail_on = fail_on
        self.on_refresh = on_refresh
        self.added: list[Any] = []
        self.commits = 0
        self.flushes = 0
        self.rollbacks = 0
        self.refreshed: list[Any] = []
        self.statements: list[Any] = []
        self.executed: list[tuple[Any, dict[str, Any] | None]] = []
        self.scalars_calls = 0

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: Any, pk: Any) -> Any:
        return self._get.pop(0) if self._get else None

    async def scalar(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        return self._scalar.pop(0) if self._scalar else None

    async def scalars(self, stmt: Any) -> FakeResult:
        self.statements.append(stmt)
        self.scalars_calls += 1
        if self.fail_on == "scalars":
            self.fail_on = None
            raise RuntimeError("巡检一次失败")
        return FakeResult(self._scalars.pop(0) if self._scalars else [])

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> FakeResult:
        self.statements.append(stmt)
        self.executed.append((stmt, params))
        return FakeResult(self._execute.pop(0) if self._execute else [])

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            err, self.commit_error = self.commit_error, None
            raise err

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)
        if self.on_refresh is not None:
            self.on_refresh(obj)

    def add(self, obj: Any) -> None:
        self.added.append(obj)


class RoutingSession:
    """按语句路由的只读会话替身。

    `get(model, pk)` 在 `entities` 里按 `isinstance` + `id` 命中（命中不到给 `None`）；
    `scalars` / `execute` 先问 `router(stmt)`，它返回 `None` 时分别回落到
    `scalar_rows` / `execute_rows`。路由写成闭包而非数据表，是为了让每个用例把
    "哪条语句拿哪些行" 就地写清，避免把测试语义藏进共享层的匹配规则里。

    记账：`statements`（全部下发语句，按顺序）、`executed`（`(stmt, params)`）、
    `scalars_calls` / `execute_calls`。
    """

    def __init__(
        self,
        *,
        entities: list[Any] | None = None,
        router: Callable[[Any], list[Any] | None] | None = None,
        scalar_rows: list[Any] | None = None,
        execute_rows: list[Any] | None = None,
    ) -> None:
        self.entities = list(entities or [])
        self.router = router
        self.scalar_rows = list(scalar_rows or [])
        self.execute_rows = list(execute_rows or [])
        self.statements: list[Any] = []
        self.executed: list[tuple[Any, dict[str, Any] | None]] = []
        self.scalars_calls = 0
        self.execute_calls = 0

    def _route(self, stmt: Any, fallback: list[Any]) -> FakeResult:
        if self.router is not None:
            rows = self.router(stmt)
            if rows is not None:
                return FakeResult(rows)
        return FakeResult(fallback)

    async def __aenter__(self) -> "RoutingSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: Any, pk: Any) -> Any:
        for row in self.entities:
            if isinstance(row, model) and getattr(row, "id", None) == pk:
                return row
        return None

    async def scalars(self, stmt: Any) -> FakeResult:
        self.statements.append(stmt)
        self.scalars_calls += 1
        return self._route(stmt, self.scalar_rows)

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> FakeResult:
        self.statements.append(stmt)
        self.executed.append((stmt, params))
        self.execute_calls += 1
        return self._route(stmt, self.execute_rows)

    async def commit(self) -> None:
        return None


class RecordingSession:
    """只记写入的最小会话替身。

    `get` 固定返回构造时预置的行（未给则 `None`）；`execute` 把
    `(str(statement), params or {})` 追加进 `statements`，`add` 追加进 `added`，
    `commit` / `flush` 只计数，`refresh` 记进 `refreshed`，`scalars` 恒为空
    （写路径用例不该依赖查询结果）。SQL 断言一律读 `statements`。
    """

    def __init__(self, get_row: Any = None) -> None:
        self.get_row = get_row
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.added: list[Any] = []
        self.commits = 0
        self.flushes = 0
        self.refreshed: list[Any] = []

    async def __aenter__(self) -> "RecordingSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, model: Any, pk: Any) -> Any:
        return self.get_row

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        self.statements.append((str(statement), params or {}))

    async def scalars(self, stmt: Any) -> FakeResult:
        return FakeResult([])

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1
