"""注册期容量预警（方案 §4 P0-2 增量）：展开工具数 vs 目标 Agent 的 tool_budget。

三条口径：
1. 纯函数比对（含 None / 0 预算的运行时同源兜底）；
2. 名字不存在如实上报，不静默跳过；
3. 两条注册路径（平台 `POST /workers/{name}/versions`、开放 API `POST /open/workers/register`）
   只出 warnings，**不阻断**。
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from app.modules.agents.models import Agent
from app.modules.capabilities.capacity import (
    DEFAULT_TOOL_BUDGET,
    budget_warnings,
    target_agent_warnings,
)
from app.modules.discovery.assembler import MAX_TOOLS_HARD
from tests.support.fake_db import RoutingSession


def _db(*, caps: list[Any] | None = None, agents: list[Any] | None = None) -> RoutingSession:
    """只读替身：按查询实体分派（Agent 查询给 `agents`，其余给 `caps`），并记录查询次数。"""
    caps = caps or []
    agents = agents or []

    def route(stmt: Any) -> list[Any]:
        return agents if stmt.column_descriptions[0]["entity"] is Agent else caps

    return RoutingSession(router=route)


class _AgentRow:
    def __init__(self, name: str, tool_budget: Any) -> None:
        self.name = name
        self.tool_budget = tool_budget


class _CapRow:
    """能力行 → cap_dict 只需 `payload.schema.function.name` 与几个字段。"""

    def __init__(self, name: str, index: int) -> None:
        self.id = uuid4()
        self.name = name
        self.type = "tool"
        self.description = ""
        self.risk_level = "read"
        self.payload = {"schema": {"type": "function", "function": {"name": name}}}
        self.index = index


# ---------- 1. 纯函数 ----------


def test_budget_warnings_flags_only_agents_that_cannot_fit() -> None:
    out = budget_warnings(14, {"装不下的": 8, "刚好够": 14, "宽裕": 24})
    assert len(out) == 1
    assert "「装不下的」" in out[0] and "tool_budget=8" in out[0]
    assert "14 个工具" in out[0] and "至少 6 个工具" in out[0]


def test_budget_warnings_order_is_deterministic() -> None:
    out = budget_warnings(9, {"乙": 8, "甲": 8, "丙": 8})
    assert [w.split("「")[1].split("」")[0] for w in out] == ["丙", "乙", "甲"]


def test_budget_warnings_uses_runtime_default_for_empty_budget() -> None:
    """NULL / 0 预算按运行时兜底 8 算（graph.py 的 `tool_budget or 8` 同源）。"""
    out = budget_warnings(9, {"空值": None, "零值": 0, "够用": 9})  # type: ignore[dict-item]
    assert len(out) == 2
    assert all(f"tool_budget={DEFAULT_TOOL_BUDGET}" in w for w in out)
    assert DEFAULT_TOOL_BUDGET == 8


def test_budget_warnings_empty_when_nothing_to_compare() -> None:
    assert budget_warnings(24, {}) == []
    assert budget_warnings(0, {"任何": 1}) == []


# ---------- 2. DB 取数（含名字不存在） ----------


def test_target_agent_warnings_reads_budgets_and_reports_unknown() -> None:
    db = _db(agents=[_AgentRow("小助", 6), _AgentRow("调研员", None)])
    out = asyncio.run(target_agent_warnings(db, 14, [" 小助 ", "调研员", "查无此Agent", ""]))
    assert db.scalars_calls == 1  # 一次批量查询，不按名字 N 次
    assert len(out) == 3
    assert "「小助」" in out[0] and "tool_budget=6" in out[0]
    assert "「调研员」" in out[1] and f"tool_budget={DEFAULT_TOOL_BUDGET}" in out[1]
    assert out[2] == "目标 Agent「查无此Agent」不存在：跳过预算比对"


def test_target_agent_warnings_skips_db_when_no_names() -> None:
    db = _db()
    assert asyncio.run(target_agent_warnings(db, 14, [])) == []
    assert asyncio.run(target_agent_warnings(db, 14, ["", "   "])) == []
    assert db.scalars_calls == 0


# ---------- 3. 开放 API 注册路径（非阻断） ----------


def _bundle_body(names: list[str], target_agents: list[str]) -> Any:
    from app.modules.open_api.schemas import OpenWorkerIn, OpenWorkerRegisterIn

    return OpenWorkerRegisterIn(
        worker=OpenWorkerIn(
            name="预算预警Worker", description="d", capabilities=names, playbook="# x\n"
        ),
        capabilities=[],
        target_agents=target_agents,
    )


def _stub_register_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.open_api import service as open_api_service

    async def _fake(_db: Any, _caps: Any) -> list[Any]:
        return []

    monkeypatch.setattr(open_api_service, "_register_capabilities", _fake)


def test_register_bundle_reports_budget_warning_without_blocking(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装不下只报警告：仍返回 created（硬门之外的依赖关系由配置者自己负责）。"""
    from app.modules.open_api.service import register_bundle
    from app.modules.workers import registry

    _stub_register_capabilities(monkeypatch)
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    try:
        names = [f"cap_{i:02d}" for i in range(MAX_TOOLS_HARD)]
        db = _db(
            caps=[_CapRow(n, i) for i, n in enumerate(names)],
            agents=[_AgentRow("小助", 8)],
        )
        out = asyncio.run(register_bundle(db, _bundle_body(names, ["小助"])))
        assert out.action == "created"
        assert any("tool_budget=8" in w and "「小助」" in w for w in out.warnings)
    finally:
        registry._invalidate_all()


def test_register_bundle_without_target_agents_is_unchanged(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省零影响：不填名单就不查 Agent，也不产生容量警告。"""
    from app.modules.open_api.service import register_bundle
    from app.modules.workers import registry

    _stub_register_capabilities(monkeypatch)
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    try:
        names = ["cap_00", "cap_01"]
        db = _db(caps=[_CapRow(n, i) for i, n in enumerate(names)])
        out = asyncio.run(register_bundle(db, _bundle_body(names, [])))
        assert out.warnings == []

        body = _bundle_body(names, [])
        assert body.target_agents == []

        # 老客户端完全不传该字段（缺省 = 零影响）
        from app.modules.open_api.schemas import OpenWorkerRegisterIn

        legacy = OpenWorkerRegisterIn.model_validate(
            {
                "worker": {"name": "老客户端Worker", "description": "d", "capabilities": names},
            }
        )
        assert legacy.target_agents == []
    finally:
        registry._invalidate_all()


# ---------- 4. 平台发布版本路径 ----------


def _build_version(name: str, body: Any, db: Any) -> Any:
    from app.modules.workers.router import build_version

    return asyncio.run(build_version(name, body, db))


@pytest.fixture()
def worker_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.modules.workers import registry

    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    yield registry
    registry._invalidate_all()


def test_build_version_reports_budget_warning(
    worker_root: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.workers import router as workers_router
    from app.modules.workers.schemas import VersionBuildIn

    async def _fake_count(_db: Any, _names: list[str]) -> int:
        return 14

    monkeypatch.setattr(workers_router, "_expanded_tool_count", _fake_count)
    worker_root.ensure_worker("预算预警Worker", description="d", playbook="# x\n")

    db = _db(agents=[_AgentRow("小助", 8)])
    out = _build_version("预算预警Worker", VersionBuildIn(target_agents=["小助"]), db)
    assert out.tool_count == 14
    assert len(out.warnings) == 1 and "tool_budget=8" in out.warnings[0]


def test_build_version_without_body_keeps_previous_behavior(
    worker_root: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不带请求体（老调用方）→ 不查 Agent、warnings 为空。"""
    from app.modules.workers import router as workers_router

    async def _fake_count(_db: Any, _names: list[str]) -> int:
        return 5

    monkeypatch.setattr(workers_router, "_expanded_tool_count", _fake_count)
    worker_root.ensure_worker("无名单Worker", description="d", playbook="# x\n")

    db = _db(agents=[_AgentRow("小助", 1)])  # 就算预算很小也不该被查
    out = _build_version("无名单Worker", None, db)
    assert out.tool_count == 5
    assert out.warnings == []
    assert db.scalars_calls == 0
