"""M2-2a 冻结接口的可导入性与契约单测。"""

import asyncio

from app.modules.engine.backend import EVENT_TYPES
from app.modules.engine.hooks import (
    BudgetExceededError,
    HookChain,
    RunContext,
    ToolCallRequest,
)
from app.modules.engine.state import LoopState


def test_loop_state_keys() -> None:
    # 冻结的六个键，多一个少一个都算接口变更
    assert set(LoopState.__annotations__) == {
        "messages",
        "protected_context",
        "capability_cache",
        "plan_ref",
        "budget_state",
        "confirmation",
    }


def test_event_types_frozen() -> None:
    # message_reset：工具轮与终答轮的分隔标记（流式分段，w4）
    assert set(EVENT_TYPES) == {
        "message_delta",
        "message_reset",
        "thought",
        "tool_call",
        "tool_result",
        "plan_updated",
        "confirmation_request",
        "budget_warning",
        "run_status",
        "error",
    }


async def _hook_chain_order() -> None:
    """钩子链按装配顺序执行（audit → metering → budget → loop-detect）。"""
    calls: list[str] = []

    class StubHook:
        async def on_run_start(self, ctx: RunContext) -> None:
            calls.append(f"{ctx.run_id}:start")

        async def on_run_end(self, ctx: RunContext, status: str, result: object) -> None:
            calls.append(f"{ctx.run_id}:end:{status}")

        async def on_turn_start(self, ctx: RunContext, iteration: int) -> None: ...
        async def on_turn_end(self, ctx: RunContext, iteration: int, usage: dict) -> None: ...
        async def on_tool_call(self, ctx: RunContext, call: ToolCallRequest) -> None: ...
        async def on_tool_result(self, ctx: RunContext, result: object) -> None: ...
        async def on_context_pressure(self, ctx: RunContext, ratio: float) -> None: ...

    chain = HookChain([StubHook(), StubHook()])
    ctx = RunContext(run_id="r1", conversation_id="c1", agent_id="a1")
    await chain.on_run_start(ctx)
    await chain.on_run_end(ctx, "done", None)
    assert calls == ["r1:start", "r1:start", "r1:end:done", "r1:end:done"]


def test_hook_chain_order() -> None:
    asyncio.run(_hook_chain_order())


def test_budget_error_structure() -> None:
    err = BudgetExceededError("max_iterations", "reached 25")
    assert err.gate == "max_iterations"
    assert "max_iterations" in str(err)
