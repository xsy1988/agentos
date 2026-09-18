"""记账口径（方案 §5 P1-3）。

缺陷：`MeteringHook.on_run_end` 原先 `if status != "done": return` —— token 无论
成败都在 `on_turn_end` 累加，run_count 却只计成功 → 出现"有 token 而无 run"的
不可解释账行（`run_count=0`）。

口径（修后）：run_count 与 token 同为**全量口径**，一次 run 记一次，成败都记；
成功/失败明细在 `runs.status` 查，不再分身计数。本文件把口径冻在测试里。
"""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from app.modules.engine import hooks_impl as hooks_mod
from app.modules.engine.hooks import RunContext
from app.modules.engine.hooks_impl import MeteringHook
from tests.support.fake_db import RecordingSession

RUN_STATUSES = (
    "done",
    "failed",
    "cancelled",
    "paused_awaiting_confirm",
    "waiting_external",
)


PROVIDER_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """假 DB + 假归属解析（真实 `_provider_id` 会打 DB，本地禁连）。"""
    db = RecordingSession()
    monkeypatch.setattr(hooks_mod, "session_factory", lambda: db)

    async def _provider_id(_self: MeteringHook, ctx: RunContext) -> str | None:
        return PROVIDER_ID if ctx.agent_id else None

    monkeypatch.setattr(MeteringHook, "_provider_id", _provider_id)
    return db.statements


@pytest.mark.parametrize("status", RUN_STATUSES)
def test_run_counted_for_every_terminal_status(
    status: str, recorded: list[tuple[str, dict[str, Any]]]
) -> None:
    """任意终态都计一次 run_count（token 已全量计入，run 数不得缺席）。"""
    hook = MeteringHook()
    ctx = RunContext(str(uuid4()), str(uuid4()), "a1")

    asyncio.run(hook.on_run_end(ctx, status, None))

    assert len(recorded) == 1, f"{status} 未记账"
    sql, params = recorded[0]
    assert "model_usage_daily" in sql
    assert params["r"] == 1
    assert params["i"] == 0 and params["o"] == 0  # run 收尾不重复累加 token


def test_failed_run_with_tokens_still_yields_a_run_row(
    recorded: list[tuple[str, dict[str, Any]]],
) -> None:
    """回归验收①：失败 run 有 token → 同一行 run_count 也必须 >0。"""
    hook = MeteringHook()
    ctx = RunContext(str(uuid4()), str(uuid4()), "a1")

    async def _scenario() -> None:
        await hook.on_turn_end(ctx, 0, {"input_tokens": 100, "output_tokens": 20})
        await hook.on_run_end(ctx, "failed", None)

    asyncio.run(_scenario())

    token_rows = [p for _, p in recorded if p["i"] or p["o"]]
    run_rows = [p for _, p in recorded if p["r"]]
    assert token_rows and run_rows
    assert run_rows[-1]["r"] == 1 and run_rows[-1]["i"] == 0


def test_run_count_skipped_when_provider_unknown(
    recorded: list[tuple[str, dict[str, Any]]],
) -> None:
    """归属不可解析（ctx 未初始化）时跳过记账而非计入错账（既有行为保留）。"""
    hook = MeteringHook()
    ctx = RunContext(str(uuid4()), None, None)

    asyncio.run(hook.on_run_end(ctx, "done", None))

    assert recorded == []


def test_provider_lookup_short_circuits_without_agent_id() -> None:
    """真实的 `_provider_id`：ctx 无 agent_id 时直接返回 None（不触达 DB）。"""
    assert asyncio.run(MeteringHook()._provider_id(RunContext(str(uuid4()), None, None))) is None
