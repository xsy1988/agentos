"""EngineBackend 协议 —— engine 与外部世界的唯一出口（M2-2a 冻结）。

对应设计方案 §11 演进接缝与模块详细设计 §4 硬边界：
- Engine 不知道前端存在，只经 backend 写库和发事件
- 一期实现为进程内直连（InProcessBackend，直接操作 DB 会话）
- 二期拆进程时替换为 RPC 实现，图与钩子代码零改动
"""

from typing import Any, Protocol

# 事件类型的唯一真源（模块详细设计 §1.1.7 + message_reset/context_compacted）。
# 纪律（P0-6）：任何新增事件类型必须先登记在此，并以字符串字面量发射；
# tests/test_event_registry.py 会扫描全部发射点断言其 ⊆ 本集合。
EVENT_TYPES = (
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
    "context_compacted",
    "card",
    "capability_overflow",
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

    # ---- M7a 增量扩展（ADR-23~28：任务架构）----

    async def ensure_task_id(self, conversation_id: str) -> str | None:
        """会话 → 主任务实例 id（1 会话 = 1 主任务；无会话的 timer run 返回 None）。"""
        ...

    async def get_task_context(self, task_id: str) -> dict[str, Any] | None:
        """主任务上下文（任务卡文本 + 步骤清单），装配进 system_prompt 保护区。"""
        ...

    async def start_task_plan(self, task_id: str, run_id: str) -> list[dict[str, Any]] | None:
        """按主任务架构生成计划项（含 step_id 绑定）并启动第一步；无待办主线返回 None。"""
        ...

    async def raise_subtask(
        self,
        task_id: str,
        run_id: str,
        *,
        name: str,
        description: str = "",
        question: str | None = None,
    ) -> dict[str, Any] | None:
        """抛出支线子任务；question 非空表示阻塞型澄清（run 将暂停等答复）。"""
        ...

    async def answer_subtask(self, task_id: str, step_id: str, answer: str, run_id: str) -> bool:
        """回填用户答复并把支线置 done。"""
        ...

    async def resolve_subtask(
        self,
        task_id: str,
        step_id: str,
        *,
        action: str,
        data: Any = None,
        applied: list | None = None,
        run_id: str,
    ) -> bool:
        """回填侧边栏结构化回传（§3.5）：action=submit 置 done / cancel 置 skipped。"""
        ...

    async def finalize_task_plan(self, task_id: str, run_id: str, *, achieved: bool) -> int:
        """run 终态回写：achieved 时推进绑定主步骤至 done，返回推进条数。"""
        ...

    # ---- M9a 增量扩展（方案 §4 P0-5：结果产物一等化）----

    async def save_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        name: str | None,
        mime: str | None,
        size: int,
        storage: str,
        payload: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """落一条 run 产物，返回 `{id, kind, name, mime, size, storage}`（供拼引用行）。

        `idempotency_key` 非空且已存在时返回已有行（重放/重试不重复落库）。
        """
        ...

    async def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        """run 的产物清单（按 created_at 升序），结果卡与产物面板共用。"""
        ...
