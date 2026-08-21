"""完整图（M2-2c）—— 模块详细设计 §1.1.1。

START → intent_router →(chitchat)→ agent → END（闲聊快速通道：跳过全部装配）
                      →(task)→ context_assembly → planner → confirm_plan
confirm_plan →(approved)→ agent
             →(rejected)→ END
agent ⇄ tools（ReAct 回环，钩子全程计量/审计/熔断）
agent →(无 tool_calls)→ verify →(achieved | 回环限 3 次)→ END

确认点两处（interrupt）：任务单提交前（confirm_plan）+ 高危工具调用前（tools）。
节点是薄壳，逻辑经 runtime 依赖注入（backend/emit/hooks/run_ctx）。

interrupt 重放纪律：恢复时节点从头重放——
1. planner 生成计划与 confirm_plan 的 interrupt 拆成两个节点，重放不重复调 LLM；
2. tools 节点先查全部调用风险、interrupt 一次，恢复后才逐个执行——
   副作用（工具执行/审计事件/循环计数）只发生在 interrupt 之后，恰好一次。
"""

import json
import re
import time
from typing import Any

from langchain_core.messages import (
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.modules.engine.hooks import ToolCallRequest, ToolResultInfo
from app.modules.engine.state import LoopState

# verify 回环上限（设计 §1.1.1：验证不达标带反馈回环，计数限 3 次）
VERIFY_RETRY_LIMIT = 3

CHITCHAT_RE = re.compile(
    r"^(你好|您好|hi|hello|嗨|哈喽|在吗|在么|早上好|中午好|下午好|晚上好|晚安|再见|拜拜|谢谢|多谢|ok|okay|好的|嗯+|哈+)[!！。.~\s]*$",
    re.IGNORECASE,
)


def build_system_prompt(agent_cfg: dict[str, Any]) -> str:
    """人格三件套 + 补丁位 → 系统提示词（M3 后由 assembler 五区装配替代）。"""
    parts = [
        agent_cfg.get("soul_md") or "",
        agent_cfg.get("identity_md") or "",
        agent_cfg.get("memory_md") or "",
        agent_cfg.get("system_prompt") or "",
    ]
    return "\n\n".join(p for p in parts if p.strip())


def _extract_json(text: str) -> dict[str, Any] | None:
    """从 LLM 输出中提取 JSON 对象（容忍 markdown 代码块包裹）。"""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


async def _get_llm(runtime: Any, agent_id: str) -> tuple[Any, dict[str, Any]] | None:
    """取 (chat_model, agent_cfg)。未绑定模型返回 None。"""
    from app.modules.models_module.provider import decrypt_secret, get_chat_model

    agent_cfg = await runtime.backend.get_agent_config(agent_id)
    provider = agent_cfg.get("provider")
    if provider is None or provider.get("id") is None:
        return None
    api_key = decrypt_secret(provider["api_key_encrypted"])
    return get_chat_model(provider, api_key), agent_cfg


def build_graph(runtime: Any) -> CompiledStateGraph:
    """runtime: EngineRuntime 实例（提供 backend/emit_event/hooks/run_ctx 依赖）。"""

    def _ctx(run_id: str) -> Any:
        return runtime.get_run_ctx(run_id)

    # ---------- 节点 ----------

    async def intent_router(state: LoopState, config: RunnableConfig) -> dict:
        """规则优先 + 小模型兜底分类（设计 §1.1.1：闲聊走快速通道）。"""
        msgs = state.get("messages") or []
        text = ""
        for m in reversed(msgs):
            if getattr(m, "type", "") == "human":
                text = str(m.content)
                break
        intent = "task"
        if CHITCHAT_RE.match(text.strip()):
            intent = "chitchat"
        else:
            # 小模型兜底分类（规则未命中才调，控制成本）
            llm_pack = await _get_llm(runtime, config["configurable"]["agent_id"])
            if llm_pack is not None:
                llm, _ = llm_pack
                try:
                    resp = await llm.ainvoke(
                        [
                            SystemMessage(
                                content="你是意图分类器。判断用户输入是闲聊问候"
                                "（chitchat）还是需要执行的任务（task）。"
                                '只输出 JSON：{"intent": "task"} 或 {"intent": "chitchat"}'
                            ),
                            HumanMessage(content=text[:500]),
                        ]
                    )
                    obj = _extract_json(str(resp.content))
                    if obj and obj.get("intent") in ("task", "chitchat"):
                        intent = str(obj["intent"])
                except Exception:  # noqa: BLE001 —— 分类失败按任务处理，不阻断主链路
                    intent = "task"
        return {"protected_context": {"intent": intent}}

    async def context_assembly(state: LoopState, config: RunnableConfig) -> dict:
        """确定性装配（2c 占位版）：系统提示词 + builtin 工具全集。

        M3 替换为 discovery 检索 Top-K + pinned 并入五区装配（设计 §1.2.3）。
        """
        conf = config["configurable"]
        agent_cfg = await runtime.backend.get_agent_config(conf["agent_id"])
        tools = await runtime.backend.list_enabled_tools()
        return {
            "protected_context": {
                "intent": "task",
                "system_prompt": build_system_prompt(agent_cfg),
            },
            "capability_cache": {"tools": tools},
        }

    async def planner(state: LoopState, config: RunnableConfig) -> dict:
        """生成计划写入 plans 表，State 只存 plan_ref（计划外置）。"""
        conf = config["configurable"]
        run_id: str = conf["run_id"]
        llm_pack = await _get_llm(runtime, conf["agent_id"])
        if llm_pack is None:
            return {"plan_ref": None}
        llm, _ = llm_pack
        msgs = state.get("messages") or []
        task_text = str(msgs[-1].content) if msgs else ""
        usage: dict[str, int] = {}
        try:
            resp = await llm.ainvoke(
                [
                    SystemMessage(
                        content="你是任务规划器。把用户任务拆成 1-5 步执行计划。"
                        '只输出 JSON：{"steps": ["步骤1", "步骤2"]}'
                    ),
                    HumanMessage(content=task_text[:2000]),
                ]
            )
            obj = _extract_json(str(resp.content))
            steps = obj.get("steps") if obj else None
            if getattr(resp, "usage_metadata", None):
                usage = dict(resp.usage_metadata)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 —— 规划失败不阻断，空计划继续
            steps = None
        # planner 自身的 LLM 消耗也记账（iteration 不前进，与 verify 同模式）
        await runtime.hooks.on_turn_end(
            _ctx(run_id), _ctx(run_id).budget.get("iterations") or 0, usage
        )
        items = [
            {"seq": i + 1, "text": str(s), "status": "pending"}
            for i, s in enumerate(steps or [])
        ] or [{"seq": 1, "text": f"直接处理：{task_text[:100]}", "status": "pending"}]
        plan_ref = await runtime.backend.save_plan(run_id, items)
        await runtime.emit_event(
            run_id, "plan_updated", {"plan_ref": plan_ref, "items": items}
        )
        return {"plan_ref": plan_ref}

    async def confirm_plan(state: LoopState, config: RunnableConfig) -> dict:
        """确认点 1：任务单提交前人审（设计 §1.1.1）。"""
        conf = config["configurable"]
        items = await runtime.backend.load_plan(conf["run_id"]) or []
        answer = interrupt(
            {"reason": "plan_review", "payload": {"plan": items}}
        )
        if answer == "approved":
            return {"confirmation": None}
        return {
            "confirmation": {
                "reason": "plan_review",
                "payload": {"plan": items},
                "resolved": True,
                "answer": "rejected",
            }
        }

    async def agent(state: LoopState, config: RunnableConfig) -> dict:
        """ReAct 思考节点：闲聊=无工具单次调用；任务=绑定装配工具流式回答。"""
        conf = config["configurable"]
        run_id: str = conf["run_id"]
        thread_id: str = conf["thread_id"]
        ctx = _ctx(run_id)
        hooks = runtime.hooks

        llm_pack = await _get_llm(runtime, conf["agent_id"])
        if llm_pack is None:
            text = "当前 Agent 未绑定模型，请在设置中为 Agent 指定 model_provider 后重试。"
            await runtime.emit_event(run_id, "error", {"code": "no_model", "detail": text})
            await runtime.persist_assistant_message(thread_id, run_id, text)
            return {"messages": [AIMessageChunk(content=text)]}
        llm, agent_cfg = llm_pack

        intent = (state.get("protected_context") or {}).get("intent", "task")
        messages: list[AnyMessage] = []
        sys_prompt = (state.get("protected_context") or {}).get("system_prompt") or ""
        if sys_prompt:
            messages.append(SystemMessage(content=sys_prompt))
        # 计划状态区：每轮注入最新 todo（设计 §1.2.3 五区之五，2c 简化注入）
        if state.get("plan_ref") and intent == "task":
            items = await runtime.backend.load_plan(run_id) or []
            if items:
                todo = "; ".join(
                    f"[{it['status']}] {it['text']}" for it in items
                )
                messages.append(SystemMessage(content=f"当前执行计划：{todo}"))
        messages.extend(state.get("messages") or [])

        iteration = (ctx.budget.get("iterations") or 0) + 1
        await hooks.on_turn_start(ctx, iteration)

        tools_meta = (state.get("capability_cache") or {}).get("tools") or []
        schemas = [
            t["payload"].get("schema")
            for t in tools_meta
            if t.get("payload", {}).get("schema")
        ]

        async def _stream_once() -> tuple[AIMessageChunk, list[str], dict[str, int]]:
            """单次流式调用：返回 (完整消息, 可见文本片段, usage)。"""
            bound = (
                llm.bind_tools(schemas)
                if intent == "task" and schemas
                else llm
            )
            acc: AIMessageChunk | None = None
            visible: list[str] = []
            turn_usage: dict[str, int] = {}
            async for chunk in bound.astream(messages):
                acc = chunk if acc is None else acc + chunk
                if chunk.content:
                    visible.append(str(chunk.content))
                    await runtime.emit_event(
                        run_id, "message_delta", {"text": str(chunk.content)}
                    )
                if getattr(chunk, "usage_metadata", None):
                    turn_usage = dict(chunk.usage_metadata)  # type: ignore[arg-type]
            assert acc is not None
            return acc, visible, turn_usage

        # 思考型模型偶发只输出推理不输出可见内容（实测 Kimi K2.7）：
        # 空回复重试一次；仍为空则报错，绝不能把空 AI 消息写进 thread
        # （空 assistant 消息会被 OpenAI 兼容端点拒收，污染后续所有调用）
        try:
            final, chunks, usage = await _stream_once()
            if not final.content and not final.tool_calls:
                final, chunks, usage = await _stream_once()
        except Exception as e:  # 结构化错误：run 标记 failed 由 process_run 统一处理
            raise RuntimeError(f"模型调用失败: {type(e).__name__}: {e}") from e
        if not final.content and not final.tool_calls:
            raise RuntimeError("模型连续两次返回空回复（思考型模型偶发，请重试）")
        await hooks.on_turn_end(ctx, iteration, usage)

        # thought 事件：本轮思考选择/计划调用的工具（SSE 可见）
        if final.tool_calls:
            await runtime.emit_event(
                run_id,
                "thought",
                {"tool_calls": [{"name": c["name"], "args": c["args"]} for c in final.tool_calls]},
            )

        final_text = "".join(chunks)
        # 无 tool_calls 的终答才落库（工具轮的中间 AIMessage 不单独落消息表）
        if not final.tool_calls:
            await runtime.persist_assistant_message(thread_id, run_id, final_text)
        return {"messages": [final], "budget_state": dict(ctx.budget)}

    async def tools(state: LoopState, config: RunnableConfig) -> dict:
        """执行节点：经 capabilities 通道执行工具调用（2c 为 builtin 占位实现）。

        重放纪律：先查全部调用风险 → 高危 interrupt 一次 → 恢复后才逐个执行，
        副作用只发生在 interrupt 之后。
        """
        from app.modules.engine.tools_builtin import BUILTIN_TOOLS

        conf = config["configurable"]
        run_id: str = conf["run_id"]
        ctx = _ctx(run_id)
        hooks = runtime.hooks

        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", None) or [])

        # 1) 先取能力与风险（幂等 DB 读，重放安全）
        cap_by_name: dict[str, dict[str, Any] | None] = {}
        for c in calls:
            if c["name"] not in cap_by_name:
                cap_by_name[c["name"]] = await runtime.backend.get_capability(c["name"])

        # 2) 高危确认点（设计 §1.1.1 确认点 2）
        risky = [
            {"name": c["name"], "args": c["args"]}
            for c in calls
            if (cap_by_name[c["name"]] or {}).get("risk_level") in ("write", "dangerous")
        ]
        approved = True
        if risky:
            answer = interrupt(
                {
                    "reason": "high_risk_tool",
                    "payload": {
                        "calls": risky,
                        "risk_levels": {
                            r["name"]: (cap_by_name[r["name"]] or {}).get("risk_level")
                            for r in risky
                        },
                    },
                }
            )
            approved = answer == "approved"

        # 3) 逐个执行（interrupt 之后，恰好一次）
        results: list[ToolMessage] = []
        for c in calls:
            cap = cap_by_name.get(c["name"])
            risk = (cap or {}).get("risk_level", "read")
            req = ToolCallRequest(name=c["name"], args=dict(c["args"]), risk_level=risk)
            await hooks.on_tool_call(ctx, req)

            t0 = time.monotonic()
            if cap is None:
                content, ok = f"未知工具：{c['name']}", False
            elif not approved and risk in ("write", "dangerous"):
                content, ok = "用户拒绝执行该高危操作。", False
            else:
                builtin_key = (cap.get("payload") or {}).get("builtin")
                fn = BUILTIN_TOOLS.get(str(builtin_key)) if builtin_key else None
                if fn is None:
                    content = "该工具的执行通道在 M3 提供（当前仅 builtin 占位工具可执行）"
                    ok = False
                else:
                    try:
                        content, ok = await fn(dict(c["args"])), True
                    except Exception as e:  # noqa: BLE001 —— 工具错误包装为观察结果
                        content, ok = f"工具执行异常: {type(e).__name__}: {e}", False
            elapsed = int((time.monotonic() - t0) * 1000)

            info = ToolResultInfo(
                name=c["name"], ok=ok, content=content,
                elapsed_ms=elapsed, args_snapshot=dict(c["args"]),
            )
            await hooks.on_tool_result(ctx, info)
            results.append(
                ToolMessage(content=str(content), tool_call_id=str(c["id"]))
            )
        return {"messages": results, "budget_state": dict(ctx.budget)}

    async def verify(state: LoopState, config: RunnableConfig) -> dict:
        """独立验证 LLM：评估任务是否达成；不达标带反馈回环（限 3 次）。"""
        conf = config["configurable"]
        run_id: str = conf["run_id"]
        ctx = _ctx(run_id)
        hooks = runtime.hooks

        llm_pack = await _get_llm(runtime, conf["agent_id"])
        if llm_pack is None:
            return {}
        llm, _ = llm_pack

        msgs = state.get("messages") or []
        task_text = ""
        for m in msgs:
            if getattr(m, "type", "") == "human":
                task_text = str(m.content)
        final_reply = str(msgs[-1].content) if msgs else ""

        usage: dict[str, int] = {}
        try:
            resp = await llm.ainvoke(
                [
                    SystemMessage(
                        content="你是任务验收员。判断针对任务的最终回复是否达成目标。"
                        '只输出 JSON：{"achieved": true/false, "feedback": "未达成原因与改进建议"}'
                    ),
                    HumanMessage(
                        content=f"任务：{task_text[:1000]}\n\n最终回复：{final_reply[:2000]}"
                    ),
                ]
            )
            verdict = _extract_json(str(resp.content)) or {"achieved": True, "feedback": ""}
            if getattr(resp, "usage_metadata", None):
                usage = dict(resp.usage_metadata)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 —— 验证失败视为达成，不阻断主链路
            verdict = {"achieved": True, "feedback": ""}

        # verify 回环计数（BudgetState 扩展键 verify_retries，见 ADR-10）
        budget: dict[str, Any] = dict(state.get("budget_state") or {})
        retries = int(budget.get("verify_retries") or 0)
        budget["verify_retries"] = retries + 1
        # verify 自身的 LLM 消耗也记账（iteration 不前进）
        await hooks.on_turn_end(ctx, ctx.budget.get("iterations") or 0, usage)

        achieved = bool(verdict.get("achieved")) or retries + 1 >= VERIFY_RETRY_LIMIT
        protected = dict(state.get("protected_context") or {})
        protected["verify_verdict"] = {
            "achieved": achieved,
            "feedback": verdict.get("feedback", ""),
            "retries": retries + 1,
        }
        if not achieved:
            # 反馈注入对话区，回环让 agent 继续（设计 §1.1.1 verify 回环）
            feedback = HumanMessage(
                content=f"[验收反馈] 任务尚未达成：{verdict.get('feedback', '')}。请继续完成。"
            )
            return {
                "messages": [feedback],
                "budget_state": budget,
                "protected_context": protected,
            }
        return {"budget_state": budget, "protected_context": protected}

    # ---------- 条件边 ----------

    def route_intent(state: LoopState) -> str:
        if (state.get("protected_context") or {}).get("intent") == "chitchat":
            return "chitchat"
        return "task"

    def route_confirm(state: LoopState) -> str:
        conf = state.get("confirmation")
        if conf and conf.get("answer") == "rejected":
            return END
        return "agent"

    def route_agent(state: LoopState) -> str:
        msgs = state.get("messages") or []
        last = msgs[-1] if msgs else None
        if getattr(last, "tool_calls", None):
            return "tools"
        if (state.get("protected_context") or {}).get("intent") == "chitchat":
            return END
        return "verify"

    def route_verify(state: LoopState) -> str:
        verdict = (state.get("protected_context") or {}).get("verify_verdict") or {}
        if verdict.get("achieved"):
            return END
        return "agent"

    # ---------- 装配 ----------

    g = StateGraph(LoopState)
    g.add_node("intent_router", intent_router)
    g.add_node("context_assembly", context_assembly)
    g.add_node("planner", planner)
    g.add_node("confirm_plan", confirm_plan)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_node("verify", verify)
    g.add_edge(START, "intent_router")
    g.add_conditional_edges(
        "intent_router", route_intent, {"chitchat": "agent", "task": "context_assembly"}
    )
    g.add_edge("context_assembly", "planner")
    g.add_edge("planner", "confirm_plan")
    g.add_conditional_edges("confirm_plan", route_confirm, {END: END, "agent": "agent"})
    g.add_conditional_edges(
        "agent", route_agent, {"tools": "tools", "verify": "verify", END: END}
    )
    g.add_edge("tools", "agent")
    g.add_conditional_edges("verify", route_verify, {END: END, "agent": "agent"})
    return g.compile(checkpointer=runtime.saver)
