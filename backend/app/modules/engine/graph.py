"""完整图（M2-2c）—— 模块详细设计 §1.1.1。

START → intent_router →(chitchat)→ agent → END（闲聊快速通道：跳过全部装配）
                      →(task·simple)→ context_assembly → agent（简单任务直通：
                          跳过 planner/confirm_plan/verify——识别图片、问答等单步任务
                          不需要任务清单与验收，agent 自身可调工具补台）
                      →(task·complex)→ context_assembly → planner → confirm_plan
confirm_plan →(approved)→ agent
             →(rejected)→ END
agent ⇄ tools（ReAct 回环，钩子全程计量/审计/熔断；高危工具确认点仍生效）
agent →(无 tool_calls)→ verify（仅 complex；闲聊/简单任务直达 END）→(achieved | 回环限 3 次)→ END

确认点两处（interrupt）：任务单提交前（confirm_plan，仅 complex）+ 高危工具调用前（tools）。
节点是薄壳，逻辑经 runtime 依赖注入（backend/emit/hooks/run_ctx）。

interrupt 重放纪律：恢复时节点从头重放——
1. planner 生成计划与 confirm_plan 的 interrupt 拆成两个节点，重放不重复调 LLM；
2. tools 节点先查全部调用风险、interrupt 一次，恢复后才逐个执行——
   副作用（工具执行/审计事件/循环计数）只发生在 interrupt 之后，恰好一次。
"""

import asyncio
import contextlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable
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

from app.core.config import settings
from app.modules.engine.hooks import (
    ToolCallRequest,
    ToolFailureLoopError,
    ToolResultInfo,
)
from app.modules.engine.state import LoopState
from app.modules.engine.tool_outcome import (
    ToolError,
    classify_exception,
    failure_content,
    failure_error,
    next_failure_streak,
    normalize,
)

# verify 回环上限（设计 §1.1.1：验证不达标带反馈回环，计数限 3 次）
VERIFY_RETRY_LIMIT = 3

CHITCHAT_RE = re.compile(
    r"^(你好|您好|hi|hello|嗨|哈喽|在吗|在么|早上好|中午好|下午好|晚上好|晚安|再见|拜拜|谢谢|多谢|ok|okay|好的|嗯+|哈+)[!！。.~\s]*$",
    re.IGNORECASE,
)

# 意图分类结果缓存（归一化文本+是否带图 → 分类）：重复指令常见（「重试」「继续」），
# 命中则零延迟直达。LRU 上限防膨胀；asyncio 单事件循环内访问，无需加锁。
_INTENT_CACHE: OrderedDict[str, tuple[str, str]] = OrderedDict()
_INTENT_CACHE_MAX = 256


def build_system_prompt(agent_cfg: dict[str, Any]) -> str:
    """人格三件套 + 补丁位 → 系统提示词（M3 后由 assembler 五区装配替代）。"""
    parts = [
        agent_cfg.get("soul_md") or "",
        agent_cfg.get("identity_md") or "",
        agent_cfg.get("memory_md") or "",
        agent_cfg.get("system_prompt") or "",
    ]
    return "\n\n".join(p for p in parts if p.strip())


def _human_text(content: Any) -> str:
    """从消息 content 提取纯文本（兼容多模态列表：取 text 块，图片折叠占位）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text") or ""))
                elif b.get("type") == "image_url":
                    parts.append("[图片]")
        return "".join(parts)
    return str(content or "")


def _adapt_multimodal(
    msgs: list[AnyMessage], supports_vision: bool, keep_recent: int = 2
) -> list[AnyMessage]:
    """发送视图适配（只改内存视图，不动 checkpoint）：图片块按当前 LLM 能力取舍。

    - 模型不支持视觉（provider params.vision 未开）：image_url 块替换为说明性占位，
      否则 OpenAI 兼容端点直接拒收；占位必须写明原因，让 Agent 告知用户换
      vision 模型而不是去翻找 OCR 工具（实测教训：含糊占位会诱发工具自救）；
    - 支持：仅保留最近 keep_recent 张（图片 token 昂贵，历史图片无价值折叠）。
    消息对象与 checkpoint 共享，替换时必须构造副本（model_copy）。
    """
    if not any(isinstance(getattr(m, "content", None), list) for m in msgs):
        return msgs
    budget = keep_recent if supports_vision else 0
    out: list[AnyMessage] = []
    for m in reversed(msgs):
        content = getattr(m, "content", None)
        if isinstance(content, list):
            new_blocks: list[Any] = []
            changed = False
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image_url":
                    changed = True
                    if budget > 0:
                        budget -= 1
                        new_blocks.append(b)
                    elif supports_vision:
                        new_blocks.append(
                            {"type": "text", "text": "[图片：较早的历史图片已折叠省略]"}
                        )
                    else:
                        new_blocks.append(
                            {
                                "type": "text",
                                "text": (
                                    "[图片附件：当前对话使用的模型不支持视觉输入，图片内容不可见。"
                                    "请直接告知用户换用支持视觉的模型后重新发送图片，"
                                    "不要尝试用文件工具读取或检索 OCR 工具。]"
                                ),
                            }
                        )
                else:
                    new_blocks.append(b)
            if changed:
                m = m.model_copy(update={"content": new_blocks})
        out.append(m)
    out.reverse()
    return out


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


def build_verify_verdict(
    raw: str | None, *, verify_error: dict[str, Any] | None, retries: int
) -> dict[str, Any]:
    """验收输出 → `verify_verdict`（纯函数，便于守卫"验收失败不得记为达成"）。

    - `passed`：验收员的真实结论（异常 / 输出不可解析 → False）；
    - `achieved`：是否退出回环（仅"回环次数耗尽"才强制放行，并置 `exhausted`）；
    - `verify_error`：验收自身失败的结构化原因（P0-3 可观测性）。
    """
    parsed = _extract_json(raw or "") if verify_error is None else None
    if parsed is None and verify_error is None:
        verify_error = failure_error(
            "parse_error", f"验收输出不可解析：{(raw or '')[:200]}", source="engine"
        )
    passed = bool((parsed or {}).get("achieved"))
    exhausted = retries + 1 >= VERIFY_RETRY_LIMIT
    return {
        "achieved": passed or exhausted,
        "passed": passed,
        "feedback": str((parsed or {}).get("feedback") or ""),
        "retries": retries + 1,
        "exhausted": exhausted,
        "verify_error": verify_error,
    }


async def _guarded_call(
    coro: Awaitable[Any], *, source: str
) -> tuple[Any, bool, dict[str, Any] | None]:
    """执行一次工具调用并收敛为契约 `(content, ok, error)`（方案 §4 P0-3）。

    红线：异常一律转结构化失败码（`ToolError` / `classify_exception`），
    **不得**在 except 分支里编造面向模型的自然语言"结果"。
    """
    try:
        return normalize(await coro)
    except ToolError as e:
        return None, False, e.as_error()
    except Exception as e:  # noqa: BLE001 —— 工具错误一律转结构化失败
        return None, False, classify_exception(e, source=source).as_error()


async def _get_llm(
    runtime: Any, agent_id: str, model_provider_id: str | None = None
) -> tuple[Any, dict[str, Any]] | None:
    """取 (chat_model, agent_cfg)。未绑定模型返回 None。

    model_provider_id：对话内临时换模型（run 级覆盖，随 run.input 快照固化），
    仅替换 LLM，Agent 人设/预算/工具不变。
    """
    from app.modules.models_module.provider import decrypt_secret, get_chat_model

    agent_cfg = await runtime.backend.get_agent_config(agent_id)
    provider = agent_cfg.get("provider")
    if model_provider_id:
        override = await runtime.backend.get_model_provider(model_provider_id)
        # 覆盖目标不可用时回退 Agent 绑定模型，不让整次 run 失败
        if override is not None:
            provider = override
    if provider is None or provider.get("id") is None:
        return None
    # 视觉能力标记（params.vision，设置页勾选）：agent 节点发送视图据此适配图片块
    agent_cfg = dict(agent_cfg)
    agent_cfg["vision"] = bool((provider.get("params") or {}).get("vision"))
    # 实际生效 provider id（含对话内覆盖情形）：计量归属用
    agent_cfg["provider_id"] = provider.get("id")
    api_key = decrypt_secret(provider["api_key_encrypted"])
    return get_chat_model(provider, api_key), agent_cfg


async def _get_lightweight_llm(runtime: Any) -> tuple[Any, str] | None:
    """轻量模型（params.lightweight 标记，设置页勾选）：返回 (chat_model,
    provider_id)；未配置返回 None，调用方回退主模型。闲聊回复与内部短调用
    （分类/规划/验收）用它降本。provider_id 供计量归属（token 记轻量账）。"""
    from app.modules.models_module.provider import decrypt_secret, get_chat_model

    provider = await runtime.backend.get_lightweight_provider()
    if provider is None:
        return None
    return (
        get_chat_model(provider, decrypt_secret(provider["api_key_encrypted"])),
        str(provider["id"]),
    )


async def _get_internal_llm(
    runtime: Any, agent_id: str, model_provider_id: str | None = None
) -> tuple[Any, str | None] | None:
    """内部短调用（意图分类/规划/验收）取 LLM：轻量模型优先，未配置回退主模型。

    返回 (chat_model, provider_id)；provider_id 供计量归属，None = 未解析出
    （记账回退 Agent 主模型归属）。"""
    light = await _get_lightweight_llm(runtime)
    if light is not None:
        return light
    pack = await _get_llm(runtime, agent_id, model_provider_id)
    if pack is None:
        return None
    llm, agent_cfg = pack
    return llm, agent_cfg.get("provider_id")


async def emit_task_steps(runtime: Any, task_id: str, run_id: str) -> None:
    """把主任务当前步骤作为计划事件推给前端（计划状态区与看板同源）。

    复用既有 plan_updated 事件类型（事件族不扩类，见 ADR-25）：items 即子任务清单，
    前端计划面板与左侧任务看板据此同步刷新，不需要新增事件通道。
    """
    tctx = await runtime.backend.get_task_context(task_id)
    if not tctx:
        return
    items = [
        {
            "seq": s["seq"],
            "text": s["name"],
            "status": s["status"],
            "step_id": s["id"],
            "kind": s["kind"],
        }
        for s in tctx["steps"]
    ]
    await runtime.emit_event(
        run_id,
        "plan_updated",
        {
            "plan_ref": None,
            "items": items,
            "progress": {"done": tctx["progress_done"], "total": tctx["progress_total"]},
            "task_id": task_id,
        },
    )


def build_graph(runtime: Any) -> CompiledStateGraph:
    """runtime: EngineRuntime 实例（提供 backend/emit_event/hooks/run_ctx 依赖）。"""

    def _ctx(run_id: str) -> Any:
        return runtime.get_run_ctx(run_id)

    async def _emit_task_steps(task_id: str, run_id: str) -> None:
        await emit_task_steps(runtime, task_id, run_id)

    # ---------- 节点 ----------

    async def intent_router(state: LoopState, config: RunnableConfig) -> dict:
        """规则优先 + 轻量模型兕底三分类（设计 §1.1.1：闲聊走快速通道；
        简单任务跳过规划/确认/验收，复杂任务走完整链路）。"""
        msgs = state.get("messages") or []
        text = ""
        has_image = False
        for m in reversed(msgs):
            if getattr(m, "type", "") == "human":
                content = getattr(m, "content", None)
                has_image = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "image_url" for b in content
                )
                text = _human_text(content)
                break
        intent = "task"
        complexity = "simple"
        # 预装配产物：分类与装配并行时在此就绪，供 context_assembly 直接复用
        pre_cache: dict[str, Any] | None = None
        # 缓存 key：归一化文本 + 是否带图（带图/不带图分类可能不同）
        cache_key = f"{int(has_image)}|{text.strip()[:200].lower()}"
        if CHITCHAT_RE.match(text.strip()):
            intent = "chitchat"
        elif cache_key in _INTENT_CACHE:
            intent, complexity = _INTENT_CACHE[cache_key]
            _INTENT_CACHE.move_to_end(cache_key)
        else:
            conf = config["configurable"]

            async def _classify() -> tuple[str, str, dict[str, int], str | None]:
                # 轻量模型兕底分类；只输出一个词——非流式调用延迟大头是
                # 输出 token，单词输出比 JSON 快好几倍。失败默认 simple
                # ——风险不对称时选轻路径（simple 路径 agent 仍可调工具补台）。
                # 返回 (intent, complexity, usage, provider_id)：usage 供记账
                r = await _get_internal_llm(
                    runtime, conf["agent_id"], conf.get("model_provider_id")
                )
                if r is None:
                    return "task", "simple", {}, None
                llm, pid = r
                usage: dict[str, int] = {}
                try:
                    hint = "（消息附带图片）" if has_image else ""
                    resp = await llm.ainvoke(
                        [
                            SystemMessage(
                                content="把用户输入分为三类，只输出一个词，不要解释：\n"
                                "chitchat=问候寒暄，无实质诉求\n"
                                "simple=单步可完成的任务：问答/翻译/总结/改写/"
                                "图片识别/单次查询\n"
                                "complex=多步骤拆解、跨工具编排或含写操作副作用"
                            ),
                            HumanMessage(content=f"{text[:500]}{hint}"),
                        ]
                    )
                    if getattr(resp, "usage_metadata", None):
                        usage = dict(resp.usage_metadata)  # type: ignore[arg-type]
                    word = str(resp.content).strip().lower()
                    # 匹配顺序 chitchat→complex→simple：输出偶带前缀/多余词时
                    # 按最特异的目标词优先（防 “不是 complex，是 simple” 误判 complex）
                    for w in ("chitchat", "complex", "simple"):
                        if w in word:
                            if w == "chitchat":
                                return "chitchat", "simple", usage, pid
                            return "task", w, usage, pid
                except Exception:  # noqa: BLE001 —— 分类失败按简单任务处理，不阻断主链路
                    pass
                return "task", "simple", usage, pid

            async def _pre_assemble() -> dict[str, Any] | None:
                # 语义装配与分类互不依赖（输入同为用户文本），并行执行把
                # embedding+检索延迟藏进分类等待里；失败返回 None，
                # context_assembly 兑底现场装配
                from app.modules.discovery.assembler import assemble_tools

                try:
                    agent_cfg = await runtime.backend.get_agent_config(conf["agent_id"])
                    tb = int(agent_cfg.get("tool_budget") or 8)
                    return await assemble_tools(text, conf["agent_id"], tb, conf.get("task_id"))
                except Exception:  # noqa: BLE001
                    return None

            (cls, pre_cache) = await asyncio.gather(_classify(), _pre_assemble())
            intent, complexity, cls_usage, cls_pid = cls
            _INTENT_CACHE[cache_key] = (intent, complexity)
            while len(_INTENT_CACHE) > _INTENT_CACHE_MAX:
                _INTENT_CACHE.popitem(last=False)
            # 分类调用的 token 也记账（之前漏记）：归属实际使用的 provider；
            # 记账失败不阻断分类结果
            if cls_usage:
                with contextlib.suppress(Exception):  # noqa: BLE001
                    await runtime.hooks.on_turn_end(
                        _ctx(conf["run_id"]), 0, cls_usage, provider_id=cls_pid
                    )
        # capability_cache 总是覆盖：预装配结果就绪则传下去（assembly 直接复用），
        # 否则传空 dict 清掉上个 run 的过期装配（assembly 会现场重装）
        return {
            "protected_context": {"intent": intent, "complexity": complexity},
            "capability_cache": pre_cache or {},
        }

    async def context_assembly(state: LoopState, config: RunnableConfig) -> dict:
        """五区装配之工具描述区（M3，设计 §1.2.3）：

        pinned 常驻 + 语义 Top-K（受 tool_budget 封顶）+ 元工具，经 discovery
        装配进 capability_cache；固定区（系统提示词）在此一并落 protected_context。
        """
        from app.modules.discovery.assembler import assemble_tools
        from app.modules.memory.service import get_protected_memories

        conf = config["configurable"]
        agent_cfg = await runtime.backend.get_agent_config(conf["agent_id"])
        msgs = state.get("messages") or []
        query = ""
        for m in reversed(msgs):
            if getattr(m, "type", "") == "human":
                query = _human_text(m.content)
                break
        tool_budget = int(agent_cfg.get("tool_budget") or 8)
        # 预装配复用（优化阶段）：intent_router 已在分类等待期间并行完成语义
        # 装配则直接用（含元工具，tools 必非空）；未预装配（规则/缓存短路、
        # 预装配失败）则现场装配，行为与原链路一致
        cache = state.get("capability_cache") or {}
        if not cache.get("tools"):
            cache = await assemble_tools(query, conf["agent_id"], tool_budget, conf.get("task_id"))
        # 记忆注入（M5，模块详细设计 §2.6）：platform 全文 + 最近 2 天 daily
        # 每个 run 自动携带“我是谁 + 最近发生了什么”
        memories = await get_protected_memories()
        system_prompt = build_system_prompt(agent_cfg)
        if memories:
            system_prompt = (
                f"{system_prompt}\n\n# 长期记忆（平台记忆与近期日记忆，供参考）\n{memories}"
            )
        # 技能注入（M6，模块详细设计 §1.4）：语义命中的 SKILL.md 拼进
        # system_prompt 的「可用技能」区，Agent 可参照其步骤执行
        skill_mds = cache.get("skills") or []
        if skill_mds:
            system_prompt = (
                f"{system_prompt}\n\n# 可用技能（以下技能与当前任务高度相关，"
                "请参照其步骤与注意事项执行）\n" + "\n\n---\n\n".join(skill_mds)
            )
        # 任务架构区（M7a，ADR-23/24）：主任务目标 + 子任务清单 + 进度。
        # 放在 protected_context.system_prompt 里随固定区一起注入、永不压缩——
        # 多轮会话里模型可能忘记「这是哪个主任务、还剩哪几个子任务」，
        # 该区是看板与模型共享的同一份事实。
        task_id = conf.get("task_id")
        task_ctx: dict[str, Any] | None = None
        if task_id:
            task_ctx = await runtime.backend.get_task_context(str(task_id))
            if task_ctx and task_ctx.get("card"):
                system_prompt = f"{system_prompt}\n\n{task_ctx['card']}"
        return {
            "protected_context": {
                "intent": "task",
                # 复杂度判定结果穿透装配节点（simple 分流在 assembly 之后）
                "complexity": (state.get("protected_context") or {}).get("complexity", "simple"),
                "system_prompt": system_prompt,
                "task_id": str(task_id) if task_id else None,
            },
            "capability_cache": cache,
        }

    async def planner(state: LoopState, config: RunnableConfig) -> dict:
        """生成计划写入 plans 表，State 只存 plan_ref（计划外置）。

        计划来源优先取**主任务架构**（M7a）：主任务是任务集合，其主线子任务即
        用户预先定义的执行架构，比每次让 LLM 凭空拆解更稳定、且与看板同源。
        仅当主任务无待办主线（或工具型 run 无任务）时才回退到 LLM 自由规划。
        """
        conf = config["configurable"]
        run_id: str = conf["run_id"]
        task_id = conf.get("task_id")
        if task_id:
            structured = await runtime.backend.start_task_plan(str(task_id), run_id)
            if structured:
                plan_ref = await runtime.backend.save_plan(run_id, structured)
                await runtime.emit_event(
                    run_id, "plan_updated", {"plan_ref": plan_ref, "items": structured}
                )
                return {"plan_ref": plan_ref}
        # 规划是内部短调用：轻量模型优先，未配置回退主模型
        r = await _get_internal_llm(runtime, conf["agent_id"], conf.get("model_provider_id"))
        if r is None:
            return {"plan_ref": None}
        llm, pid = r
        msgs = state.get("messages") or []
        task_text = _human_text(msgs[-1].content) if msgs else ""
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
        # planner 自身的 LLM 消耗也记账（iteration 不前进，与 verify 同模式），
        # 归属实际调用的 provider（轻量/覆盖模型）
        await runtime.hooks.on_turn_end(
            _ctx(run_id),
            _ctx(run_id).budget.get("iterations") or 0,
            usage,
            provider_id=pid,
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
        # timer 触发的 run 无人值守，等确认即死锁：计划自动批准（ADR-16）。
        # 高危工具确认（确认点 2）仍保留 interrupt，暂停后由用户回来处理。
        if conf.get("trigger") == "timer":
            return {"confirmation": None}
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

        llm_pack = await _get_llm(runtime, conf["agent_id"], conf.get("model_provider_id"))
        if llm_pack is None:
            text = "当前 Agent 未绑定模型，请在设置中为 Agent 指定 model_provider 后重试。"
            await runtime.emit_event(run_id, "error", {"code": "no_model", "detail": text})
            await runtime.persist_assistant_message(thread_id, run_id, text)
            return {"messages": [AIMessageChunk(content=text)]}
        llm, agent_cfg = llm_pack

        intent = (state.get("protected_context") or {}).get("intent", "task")
        # 本轮实际生效 provider（计量归属）：Agent 绑定/对话内覆盖的主模型，
        # 闲聊命中轻量模型时改记轻量账
        turn_pid = agent_cfg.get("provider_id")
        # 闲聊回复走轻量模型（未配置回退主模型，行为不变）
        if intent == "chitchat":
            light = await _get_lightweight_llm(runtime)
            if light is not None:
                llm, turn_pid = light
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
        # 多模态适配：图片块按当前模型视觉能力取舍（非 vision 替换占位，vision 保留最近 2 张）
        state_msgs = _adapt_multimodal(state_msgs, bool(agent_cfg.get("vision")))

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
        await hooks.on_turn_end(ctx, iteration, usage, provider_id=turn_pid)

        # thought 事件：本轮思考选择/计划调用的工具（SSE 可见）
        if final.tool_calls:
            await runtime.emit_event(
                run_id,
                "thought",
                {"tool_calls": [{"name": c["name"], "args": c["args"]} for c in final.tool_calls]},
            )
            # 轮次分隔：工具轮已流出的 message_delta 是「过程说明」而非终答，
            # 前端据此分段（中间文本随过程展示，不拼进最终回复）。
            # 实测曾有 run 把 6000+ 条中间轮 delta 全量拼进一个回复。
            await runtime.emit_event(run_id, "message_reset", {})

        final_text = "".join(chunks)
        # 无 tool_calls 的终答才落库（工具轮的中间 AIMessage 不单独落消息表）
        if not final.tool_calls:
            await runtime.persist_assistant_message(thread_id, run_id, final_text)
        # 压缩/补齐操作（若有）与终答消息一起归约进 checkpoint
        out_msgs: list[AnyMessage | RemoveMessage] = (compact_ops or []) + aborted_patches + [final]
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

        # 0) 澄清型支线（M7a/ADR-24）：先登记支线 + 暂停等答复，再回填答复。
        # 顺序是「登记 → interrupt → 回填」：登记放在 interrupt 之前，用户在看板
        # 才能立刻看到「正在等答复哪一条支线」；重放时登记按 (task, run, 标题) 复用，
        # 回填覆盖 resolution，故整段重放幂等（与 search_more_tools 同纪律）。
        # 本轮其余工具调用一律不执行：用户答复可能改变它们的参数，交给下一轮。
        ask_calls = [c for c in calls if c["name"] == "ask_user"]
        if ask_calls:
            first = ask_calls[0]
            args = dict(first["args"])
            question = str(args.get("question") or "").strip()
            title = str(args.get("title") or "").strip() or question[:40] or "向用户确认"
            task_id = conf.get("task_id")
            step_id: str | None = None
            if task_id:
                info = await runtime.backend.raise_subtask(
                    str(task_id),
                    run_id,
                    name=title,
                    description=question,
                    question=question,
                )
                step_id = (info or {}).get("step_id")
                await _emit_task_steps(str(task_id), run_id)
            answer = interrupt(
                {
                    "reason": "subtask_clarification",
                    "payload": {
                        "question": question,
                        "title": title,
                        "step_id": step_id,
                        "kind": "branch",
                    },
                }
            )
            answer_text = (
                str(answer.get("answer") or "") if isinstance(answer, dict) else str(answer)
            )
            if task_id and step_id:
                await runtime.backend.answer_subtask(str(task_id), step_id, answer_text, run_id)
                await _emit_task_steps(str(task_id), run_id)
            ask_results: list[ToolMessage] = [
                ToolMessage(
                    content=f"用户答复：{answer_text}",
                    tool_call_id=str(first["id"]),
                )
            ]
            for c in calls:
                if c["id"] == first["id"]:
                    continue
                ask_results.append(
                    ToolMessage(
                        content="（本次因等待用户澄清而中断，该调用未执行，请在下一轮重新发起）",
                        tool_call_id=str(c["id"]),
                    )
                )
            return {"messages": ask_results, "budget_state": dict(ctx.budget)}

        # 0b) 交互决策支线（决策2/§3.6 interactive_decision）：需要用户在侧边栏
        # plugin 前端里选/删/改一批数据才能继续。同 ask_user 纪律：登记支线 →
        # interrupt 抛交互决策卡（带 sidebar 描述符）→ 结构化回传回填 resolution。
        # 本轮其余工具调用一律不执行：用户处理结果可能改变它们的参数，交给下一轮。
        decision_calls = [c for c in calls if c["name"] == "request_decision"]
        if decision_calls:
            first = decision_calls[0]
            args = dict(first["args"])
            title = str(args.get("title") or "").strip() or "交互决策"
            summary = str(args.get("summary") or "").strip()
            severity = str(args.get("severity") or "info").strip()
            if severity not in ("info", "warn", "danger"):
                severity = "info"
            raw_body = args.get("body")
            body: dict[str, Any] = raw_body if isinstance(raw_body, dict) else {}
            raw_init = args.get("init_data")
            init_data: dict[str, Any] = raw_init if isinstance(raw_init, dict) else {}
            plugin_name = str(args.get("plugin") or "").strip()
            task_id = conf.get("task_id")
            step_id = None
            if task_id:
                info = await runtime.backend.raise_subtask(
                    str(task_id), run_id, name=title, description=summary
                )
                step_id = (info or {}).get("step_id")
                await _emit_task_steps(str(task_id), run_id)
            idempotency_key = f"{run_id}:{step_id or title}"
            # 侧边栏描述符（§3.5）：plugin 能力名 → frontend 清单内联，省一次前端查询；
            # 留空 plugin 则不弹侧边栏（卡片只展示 body，用户可直接跳过）。
            sidebar: dict[str, Any] | None = None
            if plugin_name:
                cap = await runtime.backend.get_capability(plugin_name)
                if cap and cap.get("type") == "plugin":
                    ctx_init = dict(init_data)
                    if task_id:
                        ctx_init.setdefault("task_id", str(task_id))
                    sidebar = {
                        "title": title,
                        "plugin_capability_id": cap.get("id"),
                        "frontend": (cap.get("payload") or {}).get("frontend"),
                        "init_data": ctx_init,
                        "width_hint": 0.5,
                        "step_id": step_id,
                        "idempotency_key": idempotency_key,
                    }
            answer = interrupt(
                {
                    "reason": "interactive_decision",
                    "payload": {
                        "card_type": "interactive_decision",
                        "title": title,
                        "summary": summary,
                        "severity": severity,
                        "body": body,
                        "actions": [
                            {
                                "key": "open",
                                "label": "去处理",
                                "kind": "open_sidebar",
                                "style": "primary",
                            },
                            {"key": "skip", "label": "跳过", "kind": "reject"},
                        ],
                        "sidebar": sidebar,
                        "step_id": step_id,
                        "idempotency_key": idempotency_key,
                        "kind": "branch",
                    },
                }
            )
            # 解析统一结构化回传（§3.5）：dict{answer, data, applied} 或裸字符串
            if isinstance(answer, dict):
                raw_action = str(answer.get("answer") or "").strip().lower()
                data = answer.get("data")
                applied = answer.get("applied")
            else:
                raw_action = str(answer).strip().lower()
                data = None
                applied = None
            action = "cancel" if raw_action in ("cancel", "rejected", "skip") else "submit"
            if task_id and step_id:
                await runtime.backend.resolve_subtask(
                    str(task_id),
                    step_id,
                    action=action,
                    data=data,
                    applied=applied,
                    run_id=run_id,
                )
                await _emit_task_steps(str(task_id), run_id)
            if action == "cancel":
                observation = f"用户跳过了该决策（{title}），未作处理，请按 playbook 兜底继续。"
            else:
                data_text = (
                    json.dumps(data, ensure_ascii=False)[:4000]
                    if data is not None
                    else "（无附加数据）"
                )
                observation = f"用户已在侧边栏处理「{title}」，结构化回传：{data_text}"
            decision_results: list[ToolMessage] = [
                ToolMessage(content=observation, tool_call_id=str(first["id"]))
            ]
            for c in calls:
                if c["id"] == first["id"]:
                    continue
                decision_results.append(
                    ToolMessage(
                        content="（本次因等待用户交互决策而中断，该调用未执行，请在下一轮重新发起）",
                        tool_call_id=str(c["id"]),
                    )
                )
            return {"messages": decision_results, "budget_state": dict(ctx.budget)}

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
        # 连续失败熔断账本（P0-3）：阈值 run 预算可覆盖，默认 settings.tool_failure_limit
        _budget: dict[str, Any] = dict(state.get("budget_state") or {})
        streak = int(_budget.get("tool_failure_streak") or 0)
        limits: dict[str, int] = ctx.limits
        limit = int(limits.get("tool_failure_limit") or settings.tool_failure_limit)
        # 跨批次保留最近失败工具名（诊断用，上限即阈值），成功一次即清零
        recent_failed: list[str] = list(_budget.get("tool_failure_tools") or [])
        last_error: dict[str, Any] | None = None
        for c in calls:
            meta = meta_by_name.get(c["name"])
            risk = (meta or {}).get("risk_level", "read")
            req = ToolCallRequest(name=c["name"], args=dict(c["args"]), risk_level=risk)
            await hooks.on_tool_call(ctx, req)

            t0 = time.monotonic()
            content: Any = None
            error: dict[str, Any] | None = None
            if meta is None:
                ok = False
                error = failure_error("invalid_args", f"模型调用了未注册的工具：{c['name']}")
            elif not approved and risk in ("write", "dangerous"):
                ok = False
                error = failure_error(
                    "denied_by_user", f"用户拒绝执行 {c['name']}（风险等级 {risk}）"
                )
            elif meta["kind"] == "meta":
                if meta["name"] == "search_more_tools":
                    from app.modules.discovery.assembler import (
                        run_search_more_tools,
                    )

                    res, ok, error = await _guarded_call(
                        run_search_more_tools(
                            dict(c["args"]),
                            conf["agent_id"],
                            state.get("capability_cache") or {},
                            conf.get("task_id"),
                        ),
                        source="engine",
                    )
                    if ok:
                        content, updated_cache = res
                elif meta["name"] == "declare_subtask":
                    title = str(c["args"].get("title") or "").strip()
                    desc = str(c["args"].get("description") or "").strip()
                    task_id = conf.get("task_id")
                    if not title:
                        ok, error = False, failure_error("invalid_args", "title 不能为空")
                    elif not task_id:
                        ok = False
                        error = failure_error("invalid_args", "当前会话没有主任务")
                    else:
                        info, ok, error = await _guarded_call(
                            runtime.backend.raise_subtask(
                                str(task_id), run_id, name=title, description=desc
                            ),
                            source="engine",
                        )
                        if ok and info is not None:
                            content = f"已登记支线子任务：{title}（B{info['seq']}）"
                            await _emit_task_steps(str(task_id), run_id)
                        elif ok:
                            ok = False
                            error = failure_error("internal_error", "raise_subtask 返回空")
                else:
                    ok = False
                    error = failure_error("invalid_args", f"未知元工具：{c['name']}")
            elif meta["kind"] == "mcp":
                from app.modules.capabilities.mcp_client import mcp_pool

                content, ok, error = await _guarded_call(
                    mcp_pool.call_tool(
                        UUID(meta["capability_id"]), meta["tool_name"], dict(c["args"])
                    ),
                    source="mcp",
                )
            else:  # builtin 占位工具（本进程内执行）
                fn = BUILTIN_TOOLS.get(str(meta.get("builtin") or ""))
                if fn is None:
                    ok = False
                    error = failure_error(
                        "internal_error", f"builtin 注册键缺失：{c['name']}"
                    )
                else:
                    # 契约：返回裸值 = 成功；失败必须抛 ToolError（P0-3）
                    content, ok, error = await _guarded_call(
                        fn(dict(c["args"])), source="builtin"
                    )
            elapsed = int((time.monotonic() - t0) * 1000)

            streak = next_failure_streak(
                streak, None if ok else str((error or {}).get("code") or "")
            )
            if ok:
                content = "" if content is None else content
                # 长观察先落产物再进上下文（P0-5）：上下文只留引用行 + 预览
                content = await runtime.save_long_output(run_id, content, name=str(c["name"]))
                recent_failed = []
            else:
                # 失败以结构化载荷进上下文，绝不降级成"看起来像结果"的自然语言
                err = error or failure_error("internal_error", "未知失败")
                content, error = failure_content(c["name"], err), err
                recent_failed = [*recent_failed, str(c["name"])][-limit:]
                last_error = err

            info = ToolResultInfo(
                name=c["name"],
                ok=ok,
                content=content,
                elapsed_ms=elapsed,
                args_snapshot=dict(c["args"]),
                error=error,
            )
            await hooks.on_tool_result(ctx, info)
            results.append(ToolMessage(content=str(content), tool_call_id=str(c["id"])))
        budget_state = dict(ctx.budget)
        budget_state["tool_failure_streak"] = streak
        budget_state["tool_failure_tools"] = recent_failed
        out: dict[str, Any] = {"messages": results, "budget_state": budget_state}
        if updated_cache is not None:
            out["capability_cache"] = updated_cache
        # 连续失败熔断（第二道闸）：本批结果已全部落妥再抛，检查点里不留悬空 tool_calls
        if streak >= limit:
            raise ToolFailureLoopError(
                str((last_error or {}).get("code") or "external_unavailable"),
                f"同一 run 内连续 {streak} 次工具失败（阈值 {limit}）：{', '.join(recent_failed)}",
                source=str((last_error or {}).get("source") or "builtin"),
                tools=recent_failed,
            )
        return out

    async def verify(state: LoopState, config: RunnableConfig) -> dict:
        """独立验证 LLM：评估任务是否达成；不达标带反馈回环（限 3 次）。"""
        conf = config["configurable"]
        run_id: str = conf["run_id"]
        ctx = _ctx(run_id)
        hooks = runtime.hooks

        llm_pack = await _get_llm(runtime, conf["agent_id"], conf.get("model_provider_id"))
        if llm_pack is None:
            return {}
        llm, agent_cfg = llm_pack
        # 验收是内部短调用：轻量模型优先，未配置用主模型；pid 供计量归属
        pid = agent_cfg.get("provider_id")
        light = await _get_lightweight_llm(runtime)
        if light is not None:
            llm, pid = light

        msgs = state.get("messages") or []
        task_text = ""
        for m in msgs:
            if getattr(m, "type", "") == "human":
                task_text = str(m.content)
        final_reply = str(msgs[-1].content) if msgs else ""

        usage: dict[str, int] = {}
        verify_error: dict[str, Any] | None = None
        raw_verdict: str | None = None
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
            raw_verdict = str(resp.content)
            if getattr(resp, "usage_metadata", None):
                usage = dict(resp.usage_metadata)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001 —— 验收失败一律按"未达成"处理
            verify_error = classify_exception(e, source="engine").as_error()

        # verify 回环计数（BudgetState 扩展键 verify_retries，见 ADR-10）
        budget: dict[str, Any] = dict(state.get("budget_state") or {})
        retries = int(budget.get("verify_retries") or 0)
        budget["verify_retries"] = retries + 1
        # verify 自身的 LLM 消耗也记账（iteration 不前进），
        # 归属实际调用的 provider（轻量/覆盖模型）
        await hooks.on_turn_end(ctx, ctx.budget.get("iterations") or 0, usage, provider_id=pid)

        verdict = build_verify_verdict(raw_verdict, verify_error=verify_error, retries=retries)
        protected = dict(state.get("protected_context") or {})
        protected["verify_verdict"] = verdict
        achieved = verdict["achieved"]
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

    def route_after_assembly(state: LoopState) -> str:
        # 简单任务直通执行：跳过 planner/confirm_plan（高危工具确认点 2 仍生效）
        if (state.get("protected_context") or {}).get("complexity") != "complex":
            return "agent"
        return "planner"

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
        protected = state.get("protected_context") or {}
        # 闲聊与简单任务直达 END：verify 只为复杂任务把关
        # （对"识别图片"这类单步任务，验收是一次纯浪费的 LLM 调用）
        if protected.get("intent") == "chitchat" or protected.get("complexity") != "complex":
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
    g.add_conditional_edges(
        "context_assembly",
        route_after_assembly,
        {"planner": "planner", "agent": "agent"},
    )
    g.add_edge("planner", "confirm_plan")
    g.add_conditional_edges("confirm_plan", route_confirm, {END: END, "agent": "agent"})
    g.add_conditional_edges("agent", route_agent, {"tools": "tools", "verify": "verify", END: END})
    g.add_edge("tools", "agent")
    g.add_conditional_edges("verify", route_verify, {END: END, "agent": "agent"})
    return g.compile(checkpointer=runtime.saver)
