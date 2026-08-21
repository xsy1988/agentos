"""最小图（M2-2b）：intent_router(规则占位) → agent → END。

七节点完整版（含 context_assembly/planner/tools/verify 与确认点）在 2c 落地；
本文件冻结图骨架与节点签名风格：节点是薄壳，逻辑经 runtime 依赖注入。
"""

from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.modules.engine.state import LoopState


def build_system_prompt(agent_cfg: dict[str, Any]) -> str:
    """人格三件套 + 补丁位 → 系统提示词（M3 后由 assembler 五区装配替代）。"""
    parts = [
        agent_cfg.get("soul_md") or "",
        agent_cfg.get("identity_md") or "",
        agent_cfg.get("memory_md") or "",
        agent_cfg.get("system_prompt") or "",
    ]
    return "\n\n".join(p for p in parts if p.strip())


def build_graph(runtime: Any) -> CompiledStateGraph:
    """runtime: EngineRuntime 实例（提供 backend/emit_event 等依赖）。"""

    async def intent_router(state: LoopState) -> dict:
        """规则版分流占位：2c 接小模型兜底与闲聊快速通道。"""
        return {}

    async def agent_node(state: LoopState, config: RunnableConfig) -> dict:
        conf = config["configurable"]
        run_id: str = conf["run_id"]
        thread_id: str = conf["thread_id"]

        agent_cfg = await runtime.backend.get_agent_config(conf["agent_id"])
        provider = agent_cfg.get("provider")

        if provider is None or provider.get("id") is None:
            text = "当前 Agent 未绑定模型，请在设置中为 Agent 指定 model_provider 后重试。"
            await runtime.emit_event(run_id, "error", {"code": "no_model", "detail": text})
            await runtime.persist_assistant_message(thread_id, run_id, text)
            return {"messages": [AIMessage(text)]}

        from app.modules.models_module.provider import decrypt_secret, get_chat_model

        api_key = decrypt_secret(provider["api_key_encrypted"])
        llm = get_chat_model(provider, api_key)
        messages: list[AnyMessage] = [SystemMessage(content=build_system_prompt(agent_cfg))]
        messages.extend(state.get("messages", []))

        chunks: list[str] = []
        usage: dict[str, int] = {}
        try:
            async for chunk in llm.astream(messages):
                if chunk.content:
                    chunks.append(str(chunk.content))
                    await runtime.emit_event(
                        run_id, "message_delta", {"text": str(chunk.content)}
                    )
                if getattr(chunk, "usage_metadata", None):
                    usage = dict(chunk.usage_metadata)  # type: ignore[arg-type]
        except Exception as e:  # 结构化错误：run 标记 failed 由 process_run 统一处理
            raise RuntimeError(f"模型调用失败: {type(e).__name__}: {e}") from e

        final_text = "".join(chunks)
        await runtime.persist_assistant_message(thread_id, run_id, final_text)

        budget: dict[str, Any] = dict(state.get("budget_state") or {})
        prev_in = int(budget.get("input_tokens") or 0)
        prev_out = int(budget.get("output_tokens") or 0)
        budget["input_tokens"] = prev_in + (usage.get("input_tokens") or 0)
        budget["output_tokens"] = prev_out + (usage.get("output_tokens") or 0)
        return {"messages": [AIMessage(final_text)], "budget_state": budget}  # type: ignore[typeddict-item]

    g = StateGraph(LoopState)
    g.add_node("intent_router", intent_router)
    g.add_node("agent", agent_node)  # type: ignore[arg-type]
    g.add_edge(START, "intent_router")
    g.add_edge("intent_router", "agent")
    g.add_edge("agent", END)
    return g.compile(checkpointer=runtime.saver)
