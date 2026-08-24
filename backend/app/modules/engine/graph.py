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
    RemoveMessage,
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


def _normalize_tool_responses(
    msgs: list[AnyMessage],
) -> tuple[list[AnyMessage], list[ToolMessage]]:
    """规范化 tool_calls 响应：悬空补齐 + 响应吸附到对应 AIMessage 之后。

    触发场景：run 在 tools 节点 interrupt 处被 abort / kill 后，thread 里遗留
    无响应的 tool_calls；而 add_messages 归约只能 append 到末尾，补丁在
    checkpoint 里位置可能不紧贴原 AIMessage（OpenAI 端点会拒收）。发送前
    统一规范化（幂等）：每个 tool_call 的响应紧跟其 AIMessage，缺失则补
    一条 [aborted] 空响应；补丁同时写回 checkpoint（末尾位置下轮再吸附）。
    """
    resp_idx: dict[str, int] = {}
    for i, m in enumerate(msgs):
        if m.type == "tool":
            resp_idx.setdefault(str(getattr(m, "tool_call_id", "")), i)
    consumed: set[int] = set()
    new_msgs: list[AnyMessage] = []
    patches: list[ToolMessage] = []
    for i, m in enumerate(msgs):
        calls = getattr(m, "tool_calls", None) or []
        if calls:
            new_msgs.append(m)
            for tc in calls:
                tid = str(tc.get("id") or "")
                j = resp_idx.get(tid) if tid else None
                if j is not None and j > i:
                    new_msgs.append(msgs[j])
                    consumed.add(j)
                elif tid and j is None:
                    patch = ToolMessage(
                        content="[aborted] 该工具调用未执行（任务被中止），请基于现状继续。",
                        tool_call_id=tid,
                    )
                    patches.append(patch)
                    new_msgs.append(patch)
                # j < i：响应已在更早位置（乱序 checkpoint，罕见），不重复吸附
        elif i not in consumed:
            new_msgs.append(m)
    return new_msgs, patches


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
        """五区装配之工具描述区（M3，设计 §1.2.3）：

        pinned 常驻 + 语义 Top-K（受 tool_budget 封顶）+ 元工具，经 discovery
        装配进 capability_cache；固定区（系统提示词）在此一并落 protected_context。
        """
        from app.modules.discovery.assembler import assemble_tools

        conf = config["configurable"]
        agent_cfg = await runtime.backend.get_agent_config(conf["agent_id"])
        msgs = state.get("messages") or []
        query = ""
        for m in reversed(msgs):
            if getattr(m, "type", "") == "human":
                query = str(m.content)
                break
        tool_budget = int(agent_cfg.get("tool_budget") or 8)
        cache = await assemble_tools(query, conf["agent_id"], tool_budget)
        return {
            "protected_context": {
                "intent": "task",
                "system_prompt": build_system_prompt(agent_cfg),
            },
            "capability_cache": cache,
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
            {"seq": i + 1, "text": str(s), "status": "pending"} for i, s in enumerate(steps or [])
        ] or [{"seq": 1, "text": f"直接处理：{task_text[:100]}", "status": "pending"}]
        plan_ref = await runtime.backend.save_plan(run_id, items)
        await runtime.emit_event(run_id, "plan_updated", {"plan_ref": plan_ref, "items": items})
        return {"plan_ref": plan_ref}

    async def confirm_plan(state: LoopState, config: RunnableConfig) -> dict:
        """确认点 1：任务单提交前人审（设计 §1.1.1）。"""
        conf = config["configurable"]
        items = await runtime.backend.load_plan(conf["run_id"]) or []
        answer = interrupt({"reason": "plan_review", "payload": {"plan": items}})
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
        state_msgs = list(state.get("messages") or [])
        # L1/L2 压缩（估算用量 ≥ 阈值触发；保护名单不在消息区，天然安全）
        from app.modules.discovery.assembler import compact_messages, local_view

        compact_ops = await compact_messages(
            llm, state_msgs, lambda et, p: runtime.emit_event(run_id, et, p)
        )
        if compact_ops is not None:
            state_msgs = local_view(state_msgs, compact_ops)
        # 悬空 tool_calls 防御（见 _normalize_tool_responses 文档）：
        # 发送视图规范化 + 补丁写回 checkpoint
        state_msgs, aborted_patches = _normalize_tool_responses(state_msgs)

        messages: list[AnyMessage] = []
        sys_prompt = (state.get("protected_context") or {}).get("system_prompt") or ""
        if sys_prompt:
            messages.append(SystemMessage(content=sys_prompt))
        # 计划状态区：每轮注入最新 todo（设计 §1.2.3 五区之五，2c 简化注入）
        if state.get("plan_ref") and intent == "task":
            items = await runtime.backend.load_plan(run_id) or []
            if items:
                todo = "; ".join(f"[{it['status']}] {it['text']}" for it in items)
                messages.append(SystemMessage(content=f"当前执行计划：{todo}"))
        messages.extend(state_msgs)

        iteration = (ctx.budget.get("iterations") or 0) + 1
        await hooks.on_turn_start(ctx, iteration)

        tools_meta = (state.get("capability_cache") or {}).get("tools") or []
        schemas = [t["schema"] for t in tools_meta if t.get("schema")]

        async def _stream_once() -> tuple[AIMessageChunk, list[str], dict[str, int]]:
            """单次流式调用：返回 (完整消息, 可见文本片段, usage)。"""
            bound = llm.bind_tools(schemas) if intent == "task" and schemas else llm
            acc: AIMessageChunk | None = None
            visible: list[str] = []
            turn_usage: dict[str, int] = {}
            async for chunk in bound.astream(messages):
                acc = chunk if acc is None else acc + chunk
                if chunk.content:
                    visible.append(str(chunk.content))
                    await runtime.emit_event(run_id, "message_delta", {"text": str(chunk.content)})
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
        # 压缩/补齐操作（若有）与终答消息一起归约进 checkpoint
        out_msgs: list[AnyMessage | RemoveMessage] = (
            (compact_ops or []) + aborted_patches + [final]
        )
        return {"messages": out_msgs, "budget_state": dict(ctx.budget)}

    async def tools(state: LoopState, config: RunnableConfig) -> dict:
        """执行节点：按 capability_cache 条目的 kind 分派执行通道（M3）。

        - tool（builtin 占位）：本进程内 BUILTIN_TOOLS
        - mcp/plugin：mcp_pool.call_tool(capability_id, tool_name)
        - meta：search_more_tools 检索元工具（命中工具并入 capability_cache）

        重放纪律：先查全部调用风险 → 高危 interrupt 一次 → 恢复后才逐个执行，
        副作用只发生在 interrupt 之后。风险/元数据从 state.capability_cache 取
        （checkpoint 持久，比 DB 读更强的重放一致性）。
        """
        from uuid import UUID

        from app.modules.engine.tools_builtin import BUILTIN_TOOLS

        conf = config["configurable"]
        run_id: str = conf["run_id"]
        ctx = _ctx(run_id)
        hooks = runtime.hooks

        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", None) or [])
        cache_tools = (state.get("capability_cache") or {}).get("tools") or []
        meta_by_name = {t["name"]: t for t in cache_tools}

        # 1) 风险预查（幂等读 state，重放安全）
        risky = [
            {"name": c["name"], "args": c["args"]}
            for c in calls
            if (meta_by_name.get(c["name"]) or {}).get("risk_level") in ("write", "dangerous")
        ]

        # 2) 高危确认点（设计 §1.1.1 确认点 2）
        approved = True
        if risky:
            answer = interrupt(
                {
                    "reason": "high_risk_tool",
                    "payload": {
                        "calls": risky,
                        "risk_levels": {
                            r["name"]: (meta_by_name.get(r["name"]) or {}).get("risk_level")
                            for r in risky
                        },
                    },
                }
            )
            approved = answer == "approved"

        # 3) 逐个执行（interrupt 之后，恰好一次）
        results: list[ToolMessage] = []
        updated_cache: dict[str, Any] | None = None
        for c in calls:
            meta = meta_by_name.get(c["name"])
            risk = (meta or {}).get("risk_level", "read")
            req = ToolCallRequest(name=c["name"], args=dict(c["args"]), risk_level=risk)
            await hooks.on_tool_call(ctx, req)

            t0 = time.monotonic()
            if meta is None:
                content, ok = f"未知工具：{c['name']}", False
            elif not approved and risk in ("write", "dangerous"):
                content, ok = "用户拒绝执行该高危操作。", False
            else:
                try:
                    if meta["kind"] == "meta":
                        if meta["name"] == "search_more_tools":
                            from app.modules.discovery.assembler import (
                                run_search_more_tools,
                            )

                            content, updated_cache = await run_search_more_tools(
                                dict(c["args"]),
                                conf["agent_id"],
                                state.get("capability_cache") or {},
                            )
                            ok = True
                        else:
                            content, ok = f"未知元工具：{c['name']}", False
                    elif meta["kind"] == "mcp":
                        from app.modules.capabilities.mcp_client import mcp_pool

                        content = await mcp_pool.call_tool(
                            UUID(meta["capability_id"]), meta["tool_name"], dict(c["args"])
                        )
                        ok = True
                    else:  # builtin 占位工具（本进程内执行）
                        fn = BUILTIN_TOOLS.get(str(meta.get("builtin") or ""))
                        if fn is None:
                            content = f"工具执行通道缺失：{c['name']}"
                            ok = False
                        else:
                            content, ok = await fn(dict(c["args"])), True
                except Exception as e:  # noqa: BLE001 —— 工具错误包装为观察结果
                    content, ok = f"工具执行异常: {type(e).__name__}: {e}", False
            elapsed = int((time.monotonic() - t0) * 1000)

            info = ToolResultInfo(
                name=c["name"],
                ok=ok,
                content=content,
                elapsed_ms=elapsed,
                args_snapshot=dict(c["args"]),
            )
            await hooks.on_tool_result(ctx, info)
            results.append(ToolMessage(content=str(content), tool_call_id=str(c["id"])))
        out: dict[str, Any] = {"messages": results, "budget_state": dict(ctx.budget)}
        if updated_cache is not None:
            out["capability_cache"] = updated_cache
        return out

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
    g.add_conditional_edges("agent", route_agent, {"tools": "tools", "verify": "verify", END: END})
    g.add_edge("tools", "agent")
    g.add_conditional_edges("verify", route_verify, {END: END, "agent": "agent"})
    return g.compile(checkpointer=runtime.saver)
