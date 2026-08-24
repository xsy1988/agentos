"""生命周期钩子协议 —— 六钩子位（M2-2a 冻结）。

对应模块详细设计 §1.1.4。横切逻辑（审计/计量/预算/循环检测）全部挂这里，图本体保持纯净。
默认钩子链装配顺序：audit → metering → budget → loop-detect。
预算熔断是钩子而非图节点：on_turn_end / on_tool_call 中抛 BudgetExceededError 终止。
"""

from typing import Any, Protocol

from app.modules.engine.state import BudgetState


class RunContext:
    """一次 run 的执行上下文：标识与账本的载体，钩子/节点共享。"""

    def __init__(self, run_id: str, conversation_id: str | None, agent_id: str) -> None:
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.agent_id = agent_id
        self.budget: BudgetState = {
            "iterations": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_calls": 0,
            "loop_strikes": {},
        }
        # 本 run 预算上限（runs.budget 快照）：max_iterations / max_tokens_per_run /
        # timeout_seconds 等。M2-2c 扩展字段（兼容冻结协议的增量扩展，见 ADR-10）
        self.limits: dict[str, int] = {}


class HookError(Exception):
    """钩子主动终止执行的基类（预算熔断/高危拒绝等）。"""


class BudgetExceededError(HookError):
    """预算熔断：四道闸任一触发。reason 落库到 runs.error。"""

    def __init__(self, gate: str, detail: str) -> None:
        super().__init__(f"budget gate [{gate}]: {detail}")
        self.gate = gate
        self.detail = detail


class ToolCallRequest:
    """pre 钩子可见的工具调用请求。"""

    def __init__(self, name: str, args: dict[str, Any], risk_level: str = "read") -> None:
        self.name = name
        self.args = args
        self.risk_level = risk_level  # read / write / dangerous


class ToolResultInfo:
    """post 钩子可见的工具执行结果。"""

    def __init__(
        self,
        name: str,
        ok: bool,
        content: Any,
        elapsed_ms: int = 0,
        args_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.ok = ok
        self.content = content
        self.elapsed_ms = elapsed_ms
        self.args_snapshot = args_snapshot or {}  # 循环检测指纹用（M2-2c 扩展）


class EngineHook(Protocol):
    """六钩子位签名。实现方按需实现，缺省位可空实现。"""

    async def on_run_start(self, ctx: RunContext) -> None: ...

    async def on_run_end(self, ctx: RunContext, status: str, result: Any) -> None: ...

    async def on_turn_start(self, ctx: RunContext, iteration: int) -> None: ...

    async def on_turn_end(self, ctx: RunContext, iteration: int, usage: dict[str, int]) -> None: ...

    async def on_tool_call(self, ctx: RunContext, call: ToolCallRequest) -> None: ...

    """pre：审计、高危确认拦截、循环检测。拒绝时抛 HookError。"""

    async def on_tool_result(self, ctx: RunContext, result: ToolResultInfo) -> None: ...

    """post：审计、结构化错误包装、计量。"""

    async def on_context_pressure(self, ctx: RunContext, ratio: float) -> None: ...

    """上下文使用率 ≥ 0.8 时触发，驱动 assembler 压缩。"""


class HookChain:
    """钩子链：按装配顺序依次调用（audit → metering → budget → loop-detect）。

    实现方按需实现钩子位（协议缺省位可空实现），未实现的方法位直接跳过。
    """

    def __init__(self, hooks: list[EngineHook]) -> None:
        self.hooks = hooks

    async def _run(self, method: str, *args: Any) -> None:
        for hook in self.hooks:
            fn = getattr(hook, method, None)
            if fn is not None and callable(fn):
                await fn(*args)

    async def on_run_start(self, ctx: RunContext) -> None:
        await self._run("on_run_start", ctx)

    async def on_run_end(self, ctx: RunContext, status: str, result: Any) -> None:
        await self._run("on_run_end", ctx, status, result)

    async def on_turn_start(self, ctx: RunContext, iteration: int) -> None:
        await self._run("on_turn_start", ctx, iteration)

    async def on_turn_end(self, ctx: RunContext, iteration: int, usage: dict[str, int]) -> None:
        await self._run("on_turn_end", ctx, iteration, usage)

    async def on_tool_call(self, ctx: RunContext, call: ToolCallRequest) -> None:
        await self._run("on_tool_call", ctx, call)

    async def on_tool_result(self, ctx: RunContext, result: ToolResultInfo) -> None:
        await self._run("on_tool_result", ctx, result)

    async def on_context_pressure(self, ctx: RunContext, ratio: float) -> None:
        await self._run("on_context_pressure", ctx, ratio)
