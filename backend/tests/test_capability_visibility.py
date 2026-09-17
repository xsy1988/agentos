"""能力可见性硬保证（方案 §4 P0-2）：必得集永不切片 + 容量不足可观测 + 超硬上限显式失败。

四条不变量：
1. **必得集全量保留**：pinned + 主任务域内能力不参与 tool_budget 竞争，
   容量不足时宁可挤掉共享区候选，也不静默少给一个必备工具；
2. **容量不足必须有痕**：`capability_overflow` 事件 at-most-once 且带 kept/dropped，
   让"我绑了 14 个能力模型却说没有"这类问题能被直接问出来；
3. **超硬上限显式失败**：必得集 > MAX_TOOLS_HARD 时 run 落 `tool_capacity_exceeded`，
   不进入终答（静默降级只会让模型拿着残缺工具面空转）；
4. **无归属不回归**：无 pinned、无主任务域的 run，装配结果与改造前逐项一致。
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.modules.discovery import retriever as retriever_mod
from app.modules.discovery.assembler import (
    MAX_TOOLS_HARD,
    _meta_tools,
    assemble_tools,
    partition_tools,
    take_overflow_payload,
)
from app.modules.engine import runtime as runtime_mod
from app.modules.engine.hooks import ToolCapacityExceededError
from app.modules.engine.runtime import EngineRuntime
from app.modules.runs.models import Run
from tests.test_run_artifacts import _FakeBackend, _FakeDB, _Hooks


def _cap(name: str) -> dict[str, Any]:
    """最小可用能力条目：type=tool 且有 payload.schema 即可展开成工具。"""
    return {
        "id": str(uuid4()),
        "name": name,
        "type": "tool",
        "description": name,
        "risk_level": "read",
        "payload": {
            "schema": {
                "type": "function",
                "function": {"name": name, "description": name, "parameters": {}},
            }
        },
    }


def _res(**over: Any) -> dict[str, Any]:
    """retrieve_capabilities 替身的返回值。"""
    base: dict[str, Any] = {"semantic": [], "pinned": [], "required": [], "scope": None}
    base.update(over)
    return base


def _patch_retrieve(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    async def _fake(
        query: str, agent_id: str, k: int = 8, task_id: str | None = None
    ) -> dict[str, Any]:
        return result

    monkeypatch.setattr(retriever_mod, "retrieve_capabilities", _fake)


# ---------- 1. partition_tools：必得集永不切片 ----------


def _assembled(plan: dict[str, Any]) -> list[str]:
    return [t["name"] for t in plan["tools"]]


def test_partition_keeps_required_whole_when_over_budget() -> None:
    """14 个必得工具 + tool_budget=8：14 个全在、共享区名额为 0、超出者进 dropped。"""
    required = [_cap(f"must_{i}") for i in range(14)]
    shared = [_cap(f"cand_{i}") for i in range(5)]
    plan = partition_tools(required, shared, tool_budget=8)

    assert _assembled(plan) == [f"must_{i}" for i in range(14)]
    assert plan["kept"] == []
    assert plan["dropped"] == [f"cand_{i}" for i in range(5)]
    assert plan["overflow"] is True
    assert plan["reason"] == "tool_budget_insufficient"
    assert plan["hard_exceeded"] is False


def test_partition_keeps_required_when_over_budget_by_one() -> None:
    """tool_budget=6、必得集 7：装配长度 ≥7 且 dropped 非空。"""
    required = [_cap(f"must_{i}") for i in range(7)]
    plan = partition_tools(required, [_cap(f"cand_{i}") for i in range(3)], tool_budget=6)

    assert len(plan["tools"]) == 7
    assert plan["kept"] == []
    assert plan["dropped"] == ["cand_0", "cand_1", "cand_2"]


def test_partition_fills_shared_with_remaining_quota_in_order() -> None:
    """必得集 3 + 预算 6：共享区按传入顺序（分层距离序）取前 3 个，其余留痕。"""
    required = [_cap(f"must_{i}") for i in range(3)]
    shared = [_cap(f"cand_{i}") for i in range(5)]
    plan = partition_tools(required, shared, tool_budget=6)

    assert _assembled(plan) == [f"must_{i}" for i in range(3)] + ["cand_0", "cand_1", "cand_2"]
    assert plan["kept"] == ["cand_0", "cand_1", "cand_2"]
    assert plan["dropped"] == ["cand_3", "cand_4"]
    assert plan["candidates"] == [f"must_{i}" for i in range(3)] + [
        f"cand_{i}" for i in range(5)
    ]


def test_partition_hard_limit_only_counts_required() -> None:
    """硬上限只约束必得集：候选再多也不触发 hard_exceeded，只进 dropped。"""
    required = [_cap(f"must_{i}") for i in range(MAX_TOOLS_HARD)]
    plan = partition_tools(required, [_cap(f"cand_{i}") for i in range(50)], tool_budget=8)

    assert plan["hard_exceeded"] is False
    assert len(plan["tools"]) == MAX_TOOLS_HARD


def test_max_tools_hard_is_the_documented_budget() -> None:
    """硬上限是对外契约：自检接口、Worker 发布校验、文档三处同一数字。"""
    assert MAX_TOOLS_HARD == 24


# ---------- 2. 容量不足事件：at-most-once ----------


def test_overflow_payload_emitted_once_per_run() -> None:
    """首次给 payload、再次给 None：`_pre_assemble` 与 `context_assembly` 共用同一 cache。"""
    cache = {"tool_plan": partition_tools([_cap("a")], [_cap("b")], tool_budget=0)}
    first = take_overflow_payload(cache)

    assert first == {
        "reason": "tool_budget_insufficient",
        "tool_budget": 0,
        "hard_limit": MAX_TOOLS_HARD,
        "required_count": 1,
        "kept": [],
        "dropped": ["b"],
    }
    assert take_overflow_payload(cache) is None


def test_overflow_payload_none_when_within_budget() -> None:
    """装得下就静默：不得为了"有事件可看"而无病呻吟。"""
    cache = {"tool_plan": partition_tools([_cap("a")], [], tool_budget=8)}
    assert take_overflow_payload(cache) is None


def test_overflow_payload_tolerates_cache_without_plan() -> None:
    """旧版 cache（无 tool_plan，如 search_more_tools 重建的）不得抛错。"""
    assert take_overflow_payload({}) is None
    assert take_overflow_payload({"tools": []}) is None


# ---------- 3. assemble_tools：必得集全量可见 + 硬上限显式失败 ----------


def test_assemble_without_scope_matches_legacy_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 pinned、无 domain_ids：等价于改造前的 `tools[:tool_budget] + meta`（回归门）。"""
    _patch_retrieve(monkeypatch, _res(semantic=[_cap(f"cand_{i}") for i in range(12)]))
    cache = asyncio.run(assemble_tools("随便聊聊", str(uuid4()), 5))

    names = [t["name"] for t in cache["tools"]]
    assert names[:5] == [f"cand_{i}" for i in range(5)]  # 仍是分层序前 5 个
    assert names[5:] == [t["name"] for t in _meta_tools()]  # 元工具垫底、不占预算
    assert cache["tool_plan"]["dropped"] == [f"cand_{i}" for i in range(5, 12)]


def test_assemble_keeps_all_required_and_reports_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """14 个必得能力 + tool_budget=8：全部进工具面，候选全让位，事件 payload 齐全。"""
    required = [_cap(f"must_{i}") for i in range(14)]
    _patch_retrieve(monkeypatch, _res(semantic=[_cap("cand_0")], required=required))
    cache = asyncio.run(assemble_tools("对比报价", str(uuid4()), 8))

    assert [t["name"] for t in cache["tools"]][:14] == [f"must_{i}" for i in range(14)]
    payload = take_overflow_payload(cache)
    assert payload is not None
    assert payload["required_count"] == 14
    assert payload["dropped"] == ["cand_0"]
    assert take_overflow_payload(cache) is None  # 只报一次


def test_assemble_dedups_required_and_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """必得集与共享区命中同一能力只装配一次（必得集先占位，候选不重复计入）。"""
    dup = _cap("both")
    _patch_retrieve(monkeypatch, _res(semantic=[dup, _cap("other")], required=[dup]))
    cache = asyncio.run(assemble_tools("查一下", str(uuid4()), 8))

    names = [t["name"] for t in cache["tools"]]
    assert names.count("both") == 1
    assert "other" in names
    assert cache["tool_plan"]["required"] == ["both"]


def test_assemble_raises_when_required_exceeds_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """必得集 25 > 24：装配抛 `ToolCapacityExceededError`，由 run 层落失败终态。"""
    _patch_retrieve(
        monkeypatch, _res(required=[_cap(f"must_{i}") for i in range(MAX_TOOLS_HARD + 1)])
    )
    with pytest.raises(ToolCapacityExceededError) as err:
        asyncio.run(assemble_tools("查报价", str(uuid4()), 8))

    assert err.value.required == MAX_TOOLS_HARD + 1
    assert err.value.hard_limit == MAX_TOOLS_HARD
    assert len(err.value.names) == MAX_TOOLS_HARD + 1


def test_assemble_reports_hard_exceeded_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """自检通道（enforce_hard=False）：超上限也如实回报，供配置期收敛。"""
    _patch_retrieve(
        monkeypatch, _res(required=[_cap(f"must_{i}") for i in range(MAX_TOOLS_HARD + 1)])
    )
    cache = asyncio.run(assemble_tools("查报价", str(uuid4()), 8, enforce_hard=False))
    plan = cache["tool_plan"]

    assert plan["hard_exceeded"] is True
    assert len(plan["required"]) == MAX_TOOLS_HARD + 1
    assert plan["meta"] == [t["name"] for t in _meta_tools()]


# ---------- 4. 超硬上限 → run 显式失败 ----------


def _rig(monkeypatch: pytest.MonkeyPatch) -> tuple[EngineRuntime, Run, list[tuple[str, dict]]]:
    rt = EngineRuntime()
    run = Run(
        id=uuid4(),
        status="running",
        trigger="manual",
        input={},
        conversation_id=None,
        budget={},
        budget_used={},
        active_ms=0,
        started_at=datetime.now(UTC),
        deadline_at=None,
    )
    monkeypatch.setattr(runtime_mod, "session_factory", lambda: _FakeDB(run))
    rt.backend = _FakeBackend()  # type: ignore[assignment]
    rt.hooks = _Hooks()
    events: list[tuple[str, dict]] = []

    async def _emit(run_id: str, event_type: str, payload: dict) -> int:
        events.append((event_type, payload))
        return len(events)

    monkeypatch.setattr(rt, "emit_event", _emit)
    rt._build_ctx(run)
    return rt, run, events


def test_finalize_tool_capacity_fails_run_with_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超硬上限：run=failed、error.code=tool_capacity_exceeded、不可重试、带差量事实。"""
    rt, run, events = _rig(monkeypatch)
    err = ToolCapacityExceededError(25, MAX_TOOLS_HARD, [f"must_{i}" for i in range(25)])
    asyncio.run(rt._finalize_tool_capacity(str(run.id), err, phase="tools"))

    assert run.status == "failed"
    assert run.result["schema"] == "run_result/v1"
    assert run.result["outcome"] == "failed"
    assert run.error["code"] == "tool_capacity_exceeded"
    assert run.error["required_count"] == 25
    assert run.error["hard_limit"] == MAX_TOOLS_HARD
    assert run.error["retryable"] is False
    assert run.error["phase"] == "tools"
    assert "25" in run.result["text"] and "24" in run.result["text"]
    assert [t for t, _ in events] == ["error", "card", "run_status"]
    card = events[1][1]
    assert card["card_type"] == "result"
    assert card["payload"]["outcome"] == "failed"


def test_default_run_error_code_would_not_be_the_contract_code() -> None:
    """守护显式 except 分支的必要性：`_run_error` 的 code 是类名，不是契约码。"""
    err = ToolCapacityExceededError(25, MAX_TOOLS_HARD, [])
    error = runtime_mod._run_error(err, phase="tools")

    assert error["code"] == "ToolCapacityExceededError"
    assert error["code"] != "tool_capacity_exceeded"


# ---------- 第三方注册（open_api.register_bundle）容量硬门 ----------


class _CapRow:
    """`cap_dict` 只读属性，最小替身即可。"""

    def __init__(self, name: str, index: int) -> None:
        self.id = uuid4()
        self.name = name
        self.type = "tool"
        self.description = ""
        self.risk_level = "read"
        self.payload = {"schema": {"type": "function", "function": {"name": name}}}
        self.index = index


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _BundleDB:
    """只服务 register_bundle 第 2 步的 select(Capability)。"""

    def __init__(self, names: list[str]) -> None:
        self.rows = [_CapRow(n, i) for i, n in enumerate(names)]

    async def scalars(self, _stmt: Any) -> _Scalars:
        return _Scalars(self.rows)


def _bundle_body(names: list[str]) -> Any:
    from app.modules.open_api.schemas import OpenWorkerIn, OpenWorkerRegisterIn

    return OpenWorkerRegisterIn(
        worker=OpenWorkerIn(
            name="容量门Worker", description="d", capabilities=names, playbook="# x\n"
        ),
        capabilities=[],
    )


def _stub_register(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.open_api import service as open_api_service

    async def _fake(_db: Any, _caps: Any) -> list[Any]:
        return []

    monkeypatch.setattr(open_api_service, "_register_capabilities", _fake)


def _names(n: int) -> list[str]:
    return [f"cap_{i:02d}" for i in range(n)]


def test_register_bundle_rejects_worker_over_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """展开后 25 > 24：注册直接 422，且文案给出实际数量与收敛建议。"""
    from fastapi import HTTPException

    from app.modules.open_api.service import register_bundle

    _stub_register(monkeypatch)
    names = _names(MAX_TOOLS_HARD + 1)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(register_bundle(_BundleDB(names), _bundle_body(names)))

    assert ei.value.status_code == 422
    assert str(MAX_TOOLS_HARD + 1) in ei.value.detail
    assert str(MAX_TOOLS_HARD) in ei.value.detail
    assert "capabilities" in ei.value.detail


def test_register_bundle_allows_worker_at_hard_limit(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恰好等于 24：放行（边界不误伤）。"""
    from app.modules.open_api.service import register_bundle
    from app.modules.workers import registry

    _stub_register(monkeypatch)
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    try:
        names = _names(MAX_TOOLS_HARD)
        out = asyncio.run(register_bundle(_BundleDB(names), _bundle_body(names)))
        assert out.action == "created"
        assert registry.get_meta("容量门Worker") is not None
    finally:
        registry._invalidate_all()


def test_register_bundle_skips_check_without_capabilities(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无能力声明：不触碰展开逻辑（0 工具），不因硬门被拦。"""
    from app.modules.open_api.service import register_bundle
    from app.modules.workers import registry

    _stub_register(monkeypatch)
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    try:
        out = asyncio.run(register_bundle(_BundleDB([]), _bundle_body([])))
        assert out.action == "created"
    finally:
        registry._invalidate_all()
