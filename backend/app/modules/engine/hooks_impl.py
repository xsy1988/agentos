"""钩子链默认实现（M2-2c）—— 模块详细设计 §1.1.4 / §1.1.5。

装配顺序：audit → metering → budget → loop-detect（预算熔断是钩子而非图节点）。

interrupt 重放纪律（与 graph.py tools 节点配套）：
钩子的 on_tool_call / on_tool_result 只在确认点（interrupt）返回之后被调用，
因此节点重放不会重复触发钩子副作用（事件/记账/计数均恰好一次）。
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from app.core.db import session_factory
from app.modules.engine.hooks import (
    BudgetExceededError,
    RunContext,
    ToolCallRequest,
    ToolResultInfo,
)

# 同工具同参数连续调用熔断阈值（设计 §1.1.5 第三闸）
LOOP_STRIKE_LIMIT = 3

# 工具结果事件里的内容截断（run_events 是审计流不是数据搬运通道）
TOOL_RESULT_EVENT_MAX_CHARS = 500

EmitFn = Callable[[str, str, dict[str, Any]], Awaitable[int]]


class AuditHook:
    """审计：工具轨迹写 run_events（tool_call / tool_result）。"""

    def __init__(self, emit: EmitFn) -> None:
        self._emit = emit

    async def on_tool_call(self, ctx: RunContext, call: ToolCallRequest) -> None:
        await self._emit(
            ctx.run_id,
            "tool_call",
            {"name": call.name, "args": call.args, "risk_level": call.risk_level},
        )

    async def on_tool_result(self, ctx: RunContext, result: ToolResultInfo) -> None:
        content = str(result.content)
        await self._emit(
            ctx.run_id,
            "tool_result",
            {
                "name": result.name,
                "ok": result.ok,
                "elapsed_ms": result.elapsed_ms,
                "content": content[:TOOL_RESULT_EVENT_MAX_CHARS],
            },
        )


class MeteringHook:
    """计量：token 累计进 ctx.budget，分钟级 upsert model_usage_daily。"""

    def __init__(self) -> None:
        self._provider_cache: dict[str, str | None] = {}  # agent_id → provider_id

    async def _provider_id(self, ctx: RunContext) -> str | None:
        if not ctx.agent_id:
            return None  # ctx 未初始化（异常路径）：跳过记账，不炸钩子链
        if ctx.agent_id not in self._provider_cache:
            from app.modules.engine.backend_impl import InProcessBackend

            backend = InProcessBackend()
            cfg = await backend.get_agent_config(ctx.agent_id)
            provider = cfg.get("provider") or {}
            self._provider_cache[ctx.agent_id] = provider.get("id")
        return self._provider_cache[ctx.agent_id]

    async def on_turn_end(self, ctx: RunContext, iteration: int, usage: dict[str, int]) -> None:
        ctx.budget["input_tokens"] = (ctx.budget.get("input_tokens") or 0) + (
            usage.get("input_tokens") or 0
        )
        ctx.budget["output_tokens"] = (ctx.budget.get("output_tokens") or 0) + (
            usage.get("output_tokens") or 0
        )
        provider_id = await self._provider_id(ctx)
        if provider_id is None:
            return
        await self._upsert_usage(provider_id, ctx, input_delta=usage.get("input_tokens") or 0,
                                 output_delta=usage.get("output_tokens") or 0)

    async def on_run_end(self, ctx: RunContext, status: str, result: Any) -> None:
        if status != "done":
            return  # 记账只计成功 run（与设计方案 §8 统计口径一致）
        provider_id = await self._provider_id(ctx)
        if provider_id is None:
            return
        await self._upsert_usage(provider_id, ctx, run_delta=1)

    async def _upsert_usage(
        self,
        provider_id: str,
        ctx: RunContext,
        input_delta: int = 0,
        output_delta: int = 0,
        run_delta: int = 0,
    ) -> None:
        async with session_factory() as db:
            await db.execute(
                text(
                    "INSERT INTO model_usage_daily "
                    "(id, provider_id, date, input_tokens, output_tokens, total_tokens, run_count) "
                    "VALUES (gen_random_uuid(), CAST(:pid AS uuid), CURRENT_DATE, :i, :o, :t, :r) "
                    "ON CONFLICT (provider_id, date) DO UPDATE SET "
                    "input_tokens = model_usage_daily.input_tokens + :i, "
                    "output_tokens = model_usage_daily.output_tokens + :o, "
                    "total_tokens = model_usage_daily.total_tokens + :t, "
                    "run_count = model_usage_daily.run_count + :r, "
                    "updated_at = now()"
                ),
                {
                    "pid": provider_id,
                    "i": input_delta,
                    "o": output_delta,
                    "t": input_delta + output_delta,
                    "r": run_delta,
                },
            )
            await db.commit()


class BudgetHook:
    """预算熔断：四道闸中的两道在钩子里（迭代 / token），超时与循环检测另两道见下。"""

    def __init__(self, emit: EmitFn) -> None:
        self._emit = emit

    async def _trip(self, ctx: RunContext, gate: str, detail: str) -> None:
        # 先发 budget_warning 事件（SSE 可见），再抛错终止（run 落 failed 由 runtime 统一处理）
        await self._emit(
            ctx.run_id,
            "budget_warning",
            {"gate": gate, "detail": detail,
             "budget_used": dict(ctx.budget), "limits": dict(ctx.limits)},
        )
        raise BudgetExceededError(gate, detail)

    async def on_turn_end(self, ctx: RunContext, iteration: int, usage: dict[str, int]) -> None:
        ctx.budget["iterations"] = iteration
        max_iter = ctx.limits.get("max_iterations") or 25
        if iteration >= max_iter:
            await self._trip(ctx, "max_iterations", f"已达迭代上限 {max_iter}")
        max_tokens = ctx.limits.get("max_tokens_per_run") or 0
        if max_tokens > 0:
            used = (ctx.budget.get("input_tokens") or 0) + (ctx.budget.get("output_tokens") or 0)
            if used > max_tokens:
                await self._trip(ctx, "token_limit", f"token 实耗 {used} 超上限 {max_tokens}")

    async def on_tool_call(self, ctx: RunContext, call: ToolCallRequest) -> None:
        ctx.budget["tool_calls"] = (ctx.budget.get("tool_calls") or 0) + 1


class LoopDetectHook:
    """循环检测：同工具同参数连续调用 3 次 → 熔断判死循环。

    计数挂 on_tool_result（执行后恰好一次），interrupt 重放不会重复累计。
    """

    def __init__(self, emit: EmitFn) -> None:
        self._emit = emit

    async def on_tool_result(self, ctx: RunContext, result: ToolResultInfo) -> None:
        strikes: dict[str, int] = dict(ctx.budget.get("loop_strikes") or {})
        fp = f"{result.name}:{json.dumps(result.args_snapshot, sort_keys=True, ensure_ascii=False)}"
        strikes[fp] = strikes.get(fp, 0) + 1
        # 连续语义：清掉其它指纹，只保留当前
        ctx.budget["loop_strikes"] = {fp: strikes[fp]}
        if strikes[fp] >= LOOP_STRIKE_LIMIT:
            # 与 budget 闸同模式：先发 budget_warning（SSE 可见），再抛错熔断
            await self._emit(
                ctx.run_id,
                "budget_warning",
                {
                    "gate": "loop_detect",
                    "detail": f"工具 {result.name} 以相同参数连续调用 {strikes[fp]} 次，判定死循环",
                },
            )
            raise BudgetExceededError(
                "loop_detect",
                f"工具 {result.name} 以相同参数连续调用 {strikes[fp]} 次，判定死循环",
            )


def build_default_chain(emit: EmitFn) -> Any:
    """默认钩子链：audit → metering → budget → loop-detect（设计 §1.1.4）。"""
    from app.modules.engine.hooks import HookChain

    return HookChain(
        [
            AuditHook(emit),  # type: ignore[list-item]
            MeteringHook(),  # type: ignore[list-item]
            BudgetHook(emit),  # type: ignore[list-item]
            LoopDetectHook(emit),  # type: ignore[list-item]
        ]
    )
