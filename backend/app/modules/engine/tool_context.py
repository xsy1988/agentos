"""工具执行上下文（方案 §5 P1-10，Q-09「上下文传递」）。

工具调用协议扩展第 2 个参数：工具可按需声明 `ctx`，拿到**平台侧的执行归属与凭据**
（`run_id` / `task_id` / `step_id` / 幂等键 / 回调凭据），不必再从模型给的 args 里猜。

协议（两条通道统一）：

- builtin：`async def f(args, ctx)` —— 声明了第 2 个形参就注入，没声明的按 `f(args)`
  原样调用（既有工具零改动）。判定用 `inspect.signature`，**不用 try/except TypeError**
  （那会把工具内部的 TypeError 误判成"这是老签名"，把真实错误吞进适配层）。
- mcp/plugin：不经参数下发（多塞字段会撞 MCP Server 的 `inputSchema` 校验），
  走协议自带的 `tools/call` 的 `_meta` 字段（见 `ToolContext.as_meta`）。

幂等键口径：`run_id:sha256(工具名 + 规范化参数)[:32]`，与 `run_artifacts.idempotency_key`
同构——同一 run 内的重放（checkpoint 重放 / 崩溃续跑）算出同一个键，不同 run 的相同调用互不干扰。
"""

import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any

# MCP `tools/call` 的 `_meta` 载荷键（MCP 规范允许 `_meta` 携带实现自定义字段，
# 不认识它的 Server 会忽略；故意不用扁平的顶层键，避免与协议字段冲突）
CONTEXT_META_KEY = "agentos"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """一次工具调用的执行上下文（不可变，随调用传递）。"""

    run_id: str
    task_id: str | None = None
    # 本 run 直接绑定的子任务（task_steps.run_id）；一个 run 可推进多个主线步骤，
    # 故仅在确有绑定时非空，不是"当前正在做的所有步骤"
    step_id: str | None = None
    idempotency_key: str | None = None
    # 外部等待凭据（P0-4）：仅派发类建单工具的外部等待路径上有值
    callback_token: str | None = None
    await_id: str | None = None
    callback_url: str | None = None
    # 取件地址模板（P2-1）：`{file_id}` 占位，配合 await_id + callback_token 取本 run 的文件
    files_url: str | None = None

    def as_meta(self) -> dict[str, Any]:
        """MCP 通道的下发载荷：只带非空字段，空值不占位。"""
        payload = {
            f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not None
        }
        return {CONTEXT_META_KEY: payload}


def tool_idempotency_key(run_id: str, tool_name: str, args: Mapping[str, Any] | None) -> str:
    """工具调用的幂等键：`run_id:sha256(tool + canonical args)[:32]`。

    参数按 JSON 规范化（键排序）后入摘要，故键与参数键序无关；不可序列化的值退化用
    `repr`（`default=str`），不因一个怪值抛错而丢掉整次调用的凭据。
    """
    canonical = json.dumps([tool_name, dict(args or {})], sort_keys=True, default=repr)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{run_id}:{digest}"


def accepts_context(fn: Callable[..., Any]) -> bool:
    """工具是否声明了执行上下文形参（第 2 个位置参数）。"""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):  # 内建/不可自省的可调用对象：按老签名处理
        return False
    if len(params) < 2:
        return False
    return params[1].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
