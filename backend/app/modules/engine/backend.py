"""EngineBackend 协议 —— engine 与外部世界的唯一出口（M2-2a 冻结）。

对应设计方案 §11 演进接缝与模块详细设计 §4 硬边界：
- Engine 不知道前端存在，只经 backend 写库和发事件
- 一期实现为进程内直连（InProcessBackend，直接操作 DB 会话）
- 二期拆进程时替换为 RPC 实现，图与钩子代码零改动
"""

from typing import Any, Protocol

# 九类事件的合法值（模块详细设计 §1.1.7）
EVENT_TYPES = (
    "message_delta",
    "thought",
    "tool_call",
    "tool_result",
    "plan_updated",
    "confirmation_request",
    "budget_warning",
    "run_status",
    "error",
)


class EngineBackend(Protocol):
    """engine 运行时依赖的全部外部操作。签名冻结，一期进程内实现。"""

    async def get_agent_config(self, agent_id: str) -> dict[str, Any]:
        """读 Agent 配置（人格/模型绑定/预算）。变更对下一个 run 生效。"""
        ...

    async def get_model_provider(self, provider_id: str) -> dict[str, Any] | None: ...

    async def get_lightweight_provider(self) -> dict[str, Any] | None:
        """取轻量模型（params.lightweight 标记的 enabled llm，取第一个）。

        内部短调用（意图分类/规划/验收/闲聊回复）用它降本；
        未配置返回 None，调用方回退主模型。
        """
        ...

    async def emit_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """写 run_events（自动分配 run 内单调 seq）+ NOTIFY。返回 seq。"""
        ...

    async def save_plan(self, run_id: str, items: list[dict[str, Any]]) -> str:
        """upsert plans（一 run 一活动计划），返回 plan id。"""
        ...

    async def load_plan(self, run_id: str) -> list[dict[str, Any]] | None:
        """读活动计划 items（[{seq, text, status}]）。"""
        ...

    async def get_capability(self, name: str) -> dict[str, Any] | None:
        """按名取已启用的能力（含 payload/risk_level）。"""
        ...

    async def list_enabled_tools(self) -> list[dict[str, Any]]:
        """列出全部启用的 tool 类能力（context_assembly 装配用）。

        M2-2c 增量扩展（见 ADR-10）：一期返回 builtin 全集，
        M3 换 discovery 检索 Top-K + pinned 后此方法语义不变。
        """
        ...
