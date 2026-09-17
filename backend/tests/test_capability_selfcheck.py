"""能力注册自检的回归保护（P2-2）。

守住两件容易悄悄退化的事：

1. **纯前端 plugin 不进 MCP 冒烟**——它没有 MCP Server 可连（payload 只有
   `frontend` 清单），拿它去 `open_mcp_session` 会撞「未知 transport」而被判
   注册/启用失败（原方案指出的缺陷正是这个，`smoke.py` 已用 `mcp_connectable`
   跳过；这里把它固化成显式回归测试，**同时**守住"带 transport 的 plugin
   仍然真的去连"，防止跳过范围被放宽成"凡是 plugin 都跳过"）。
2. **索引只在「描述 / payload 变更」时重建**——其他字段（version / risk_level /
   enabled）变更不重建索引、不热广播，避免无意义的重算与推送。
"""

import asyncio
import contextlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.modules.capabilities import service as cap_service
from app.modules.capabilities.schemas import CapabilityUpdateIn
from app.modules.capabilities.smoke import run_smoke_test

FRONTEND_IFRAME = {"frontend": {"mode": "iframe", "url": "https://example.com/panel"}}
FRONTEND_NATIVE = {"frontend": {"mode": "server_driven", "schema": {"type": "page"}}}
HTTP_MCP = {"transport": "http", "url": "https://mcp.example.com/mcp"}


class _FakeDB:
    """只实现 update_capability / smoke 用到的那几件事。"""

    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj: Any) -> None:
        return None

    async def scalars(self, _stmt: Any) -> list[Any]:
        return []

    def add(self, _obj: Any) -> None:  # pragma: no cover - 本文件不落新工具
        raise AssertionError("本用例不应写入新行")


def _cap(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "type": "plugin",
        "description": "采购决策面板",
        "version": "1",
        "risk_level": "read",
        "payload": dict(FRONTEND_IFRAME),
        "test_info": [],
        "enabled": False,
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def mcp_guard(monkeypatch: pytest.MonkeyPatch):
    """记录/拦截 open_mcp_session 的调用。"""

    from app.modules.capabilities import mcp_client

    calls: list[dict[str, Any]] = []

    def _install(factory: Any) -> None:
        def _wrapped(payload: dict[str, Any]) -> Any:
            calls.append(dict(payload))
            return factory(payload)

        monkeypatch.setattr(mcp_client, "open_mcp_session", _wrapped)

    return SimpleNamespace(calls=calls, install=_install)


def _forbid_session(payload: dict[str, Any]) -> Any:
    raise AssertionError(f"不该进 MCP 冒烟: {payload}")


# ---------- 1. 纯前端 plugin 跳过 MCP 冒烟 ----------


@pytest.mark.parametrize("payload", [FRONTEND_IFRAME, FRONTEND_NATIVE], ids=["iframe", "native"])
def test_frontend_only_plugin_skips_mcp_smoke(mcp_guard: Any, payload: dict[str, Any]) -> None:
    mcp_guard.install(_forbid_session)
    report = asyncio.run(run_smoke_test(_FakeDB(), _cap(payload=dict(payload))))
    assert report.passed is True
    assert [c["name"] for c in report.checks] == ["前端 plugin"]
    assert "无 MCP transport" in report.checks[0]["detail"]
    assert mcp_guard.calls == []


def test_frontend_only_plugin_can_be_enabled(
    monkeypatch: pytest.MonkeyPatch, mcp_guard: Any
) -> None:
    """注册/启用链路：纯前端 plugin 冒烟通过 → enabled 置真（原缺陷的验收口径）。"""
    mcp_guard.install(_forbid_session)
    _patch_side_effects(monkeypatch)
    cap = _cap()
    db = _FakeDB()
    asyncio.run(cap_service.update_capability(db, cap, CapabilityUpdateIn(enabled=True)))
    assert cap.enabled is True
    assert db.commits == 1


# ---------- 2. 带 transport 的 plugin 仍然真的去连 ----------


def test_plugin_with_transport_still_smokes_mcp(mcp_guard: Any) -> None:
    @contextlib.asynccontextmanager
    async def _session(_payload: dict[str, Any]):
        class _S:
            async def list_tools(self) -> Any:
                return SimpleNamespace(tools=[])

        yield _S()

    mcp_guard.install(_session)
    cap = _cap(payload=dict(HTTP_MCP))
    report = asyncio.run(run_smoke_test(_FakeDB(), cap))
    assert report.passed is True
    assert [c["name"] for c in report.checks] == ["list_tools"]
    assert mcp_guard.calls == [dict(HTTP_MCP)]


def test_plugin_with_transport_reports_unreachable_server(mcp_guard: Any) -> None:
    @contextlib.asynccontextmanager
    async def _boom(_payload: dict[str, Any]):
        raise RuntimeError("connect refused")
        yield  # pragma: no cover - 仅为让本函数成为 async generator

    mcp_guard.install(_boom)
    report = asyncio.run(run_smoke_test(_FakeDB(), _cap(payload=dict(HTTP_MCP))))
    assert report.passed is False
    assert report.checks[0]["name"] == "连接 Server"
    assert "connect refused" in report.checks[0]["detail"]


def test_enable_rejected_when_smoke_fails(monkeypatch: pytest.MonkeyPatch, mcp_guard: Any) -> None:
    """冒烟不过不能启用：409 + 用户本次配置修改已提交（不回滚丢掉）。"""
    from app.modules.capabilities import smoke as smoke_mod
    from app.modules.capabilities.schemas import CapabilitySmokeReport

    async def _fail(_db: Any, _cap: Any) -> CapabilitySmokeReport:
        return CapabilitySmokeReport(passed=False, checks=[], summary="连接 Server→refused")

    monkeypatch.setattr(smoke_mod, "run_smoke_test", _fail)
    _patch_side_effects(monkeypatch)
    cap = _cap(payload=dict(HTTP_MCP))
    db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            cap_service.update_capability(
                db, cap, CapabilityUpdateIn(description="改过的描述", enabled=True)
            )
        )
    assert ei.value.status_code == 409
    assert cap.enabled is False
    assert cap.description == "改过的描述"
    assert db.commits == 1


# ---------- 3. 索引重建条件 ----------


def _patch_side_effects(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts = {"index": 0, "broadcast": 0}

    async def _index(_db: Any, _cap: Any) -> bool:
        counts["index"] += 1
        return True

    async def _broadcast(_cap_id: Any) -> None:
        counts["broadcast"] += 1

    monkeypatch.setattr(cap_service, "index_capability", _index)
    monkeypatch.setattr(cap_service, "broadcast_changed", _broadcast)
    return counts


def test_index_rebuilt_on_description_change(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _patch_side_effects(monkeypatch)
    asyncio.run(
        cap_service.update_capability(_FakeDB(), _cap(), CapabilityUpdateIn(description="新描述"))
    )
    assert counts == {"index": 1, "broadcast": 1}


def test_index_rebuilt_on_payload_change(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _patch_side_effects(monkeypatch)
    cap = _cap()
    asyncio.run(
        cap_service.update_capability(
            _FakeDB(), cap, CapabilityUpdateIn(payload=dict(FRONTEND_NATIVE))
        )
    )
    assert counts == {"index": 1, "broadcast": 1}
    assert cap.payload["frontend"]["mode"] == "server_driven"


@pytest.mark.parametrize(
    "body",
    [
        CapabilityUpdateIn(version="2"),
        CapabilityUpdateIn(risk_level="write"),
        CapabilityUpdateIn(test_info=[{"tool": "t", "input": {}}]),
    ],
    ids=["version", "risk_level", "test_info"],
)
def test_index_not_rebuilt_for_other_fields(
    monkeypatch: pytest.MonkeyPatch, body: CapabilityUpdateIn
) -> None:
    counts = _patch_side_effects(monkeypatch)
    asyncio.run(cap_service.update_capability(_FakeDB(), _cap(), body))
    assert counts == {"index": 0, "broadcast": 0}


def test_switch_only_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    """只切开关：不重建索引（描述/payload 没变），但要热广播（侧边栏可用性变了）。"""
    counts = _patch_side_effects(monkeypatch)
    cap = _cap(enabled=True)
    asyncio.run(cap_service.update_capability(_FakeDB(), cap, CapabilityUpdateIn(enabled=False)))
    assert counts == {"index": 0, "broadcast": 1}
    assert cap.enabled is False
