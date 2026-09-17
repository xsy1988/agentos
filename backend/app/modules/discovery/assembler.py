"""assembler —— 上下文五区装配 + L1/L2 两级压缩（模块详细设计 §1.2.3）。

五区：protected_context 固定区 / 技能元数据区 / 工具描述区 / 历史消息区 / 计划状态区。
本模块负责工具描述区（pinned + 语义 Top-K，tool_budget 封顶）与历史消息区压缩；
固定区（系统提示词）在 graph.build_system_prompt，计划状态区在 agent 节点注入，
技能元数据区 M4（skills_forge）接入后启用。

两级压缩（上下文估算用量 ≥ 阈值触发）：
- L1 裁剪折叠：最旧工具观察结果折叠为一行摘要
- L2 全量摘要重启：保留首尾、中间压缩成段，发 context_compacted 事件供审计
保护名单（永不压缩）：protected_context / 活跃计划 / 当前工具描述——均不在消息区，天然安全。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from app.core.config import settings
from app.modules.awaits import policy as await_policy
from app.modules.capabilities.schemas import VALID_RISK_LEVELS
from app.modules.engine import artifacts
from app.modules.engine.hooks import ToolCapacityExceededError

logger = logging.getLogger(__name__)

# 工具数量硬上限（方案 §4 P0-2）：必得集超过它即**显式失败**，不静默截断。
# 与 tool_budget 的分工：tool_budget 是"共享区名额"、可被必得集挤占；
# MAX_TOOLS_HARD 是"单个 run 能塞进上下文窗口的物理上限"，只约束必得集。
MAX_TOOLS_HARD = 24
# L1 折叠后保留原文的最近工具观察条数
COMPACT_L1_KEEP_RECENT = 4
# L1 折叠正文保留字符数（产物引用行不受此限，P0-5）
COMPACT_L1_KEEP_CHARS = 120
# L2 摘要重启保留的尾部消息条数
COMPACT_L2_KEEP_TAIL = 6

# 内置元工具：检索即工具化——Agent 觉得工具不够可自行翻找（设计 §1.2.2）
SEARCH_MORE_TOOLS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_more_tools",
        "description": (
            "当当前可用的工具不足以完成任务时，用任务关键词检索平台已注册的其他"
            "工具。返回工具名与描述；命中的工具会自动加入可用工具列表，下一轮即可"
            "直接调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索关键词或任务描述"}},
            "required": ["query"],
        },
    },
}


ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "遇到无法自行决定、必须由用户确认的事情时调用（例如识别不到供应商名称、"
            "出现无法识别的工艺、单据内部数据矛盾）。调用后本次执行立即暂停，问题会"
            "作为一条**支线子任务**登记到当前主任务并展示给用户；用户答复后执行自动"
            "继续，答复也会写入任务记录。一次只问一个问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "支线子任务的短标题，例如「确认供应商名称」",
                },
                "question": {"type": "string", "description": "要用户确认的完整问题"},
            },
            "required": ["title", "question"],
        },
    },
}

DECLARE_SUBTASK_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "declare_subtask",
        "description": (
            "执行主线任务的途中发现了新的支线工作（例如识别到新供应商需要建档、"
            "报价单外还发现了附件需要一并处理），登记到当前主任务的子任务清单，"
            "供用户在看板上看到。不阻塞当前执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "支线子任务的短标题"},
                "description": {"type": "string", "description": "这条支线要做什么"},
            },
            "required": ["title"],
        },
    },
}

REQUEST_DECISION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_decision",
        "description": (
            "遇到需要用户在**富交互界面**里处理（选/删/改一批数据）才能继续的决策点时"
            "调用（例如确认/新增供应商、确认工艺处理方案、绑定项目、数据纠错）。调用后"
            "本次执行立即暂停，聊天流抛出一张**交互决策卡**；用户点击「去处理」会在右侧"
            "侧边栏打开对应 plugin 前端页面，处理完把结果结构化回传后执行自动继续。"
            "与 ask_user 的区别：ask_user 只要一句文本答复，request_decision 需要用户"
            "在侧边栏里编辑/选择数据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "决策卡标题，例如「确认供应商建档」",
                },
                "summary": {
                    "type": "string",
                    "description": "一句话说明为何需要用户处理，例如「识别到 3 个未管理供应商」",
                },
                "severity": {
                    "type": "string",
                    "enum": ["info", "warn", "danger"],
                    "description": "卡片强调级别，默认 info",
                },
                "body": {
                    "type": "object",
                    "description": "卡片主体数据（字段/列表/表格），供用户在卡面预览待处理内容",
                },
                "plugin": {
                    "type": "string",
                    "description": (
                        "承载侧边栏前端的 plugin 能力名（须是 type=plugin 且声明了 frontend 清单"
                        "的能力）；留空则卡片只展示 body，不弹侧边栏"
                    ),
                },
                "init_data": {
                    "type": "object",
                    "description": "传给 plugin 前端的初始化数据（待处理数据/上下文）",
                },
            },
            "required": ["title"],
        },
    },
}


def _meta_tools() -> list[dict[str, Any]]:
    """元工具条目（不占 tool_budget 预算）。

    ask_user / declare_subtask / request_decision 是任务架构（ADR-24）的执行面：支线子任务的产生
    不依赖某个具体 MCP/插件的装配结果，必须任何会话都可用，故做成常驻元工具。
    """
    return [
        {
            "name": "search_more_tools",
            "schema": SEARCH_MORE_TOOLS_SCHEMA,
            "kind": "meta",
            "risk_level": "read",
            "source": "meta",
        },
        {
            "name": "ask_user",
            "schema": ASK_USER_SCHEMA,
            "kind": "meta",
            "risk_level": "read",
            "source": "meta",
        },
        {
            "name": "declare_subtask",
            "schema": DECLARE_SUBTASK_SCHEMA,
            "kind": "meta",
            "risk_level": "read",
            "source": "meta",
        },
        {
            "name": "request_decision",
            "schema": REQUEST_DECISION_SCHEMA,
            "kind": "meta",
            "risk_level": "read",
            "source": "meta",
        },
    ]


def resolve_tool_risk(
    *,
    cap_risk: str,
    tool_name: str,
    declared: dict[str, Any] | None = None,
    read_only_hint: bool = False,
    destructive_hint: bool = False,
) -> str:
    """多工具能力（mcp/plugin）的风险级细化（方案 §5 P2-4）。

    背景：`risk_level` 落在 `capabilities` 行上，而 `capability_tools` 只有开关，
    一个含写工具的 Server 被打成 `write` 后，包内只读工具（列举/查询类）也会触发
    高危确认，用户被迫对每次只读调用点"同意"。

    优先级（先声明、后退化、绝不弱化能力级 `dangerous`）：

    1. `payload.tool_risk_levels[tool_name]`：显式声明，管理员意志，最高优先；
    2. 能力级 `dangerous`：不可被任何注解"洗白"——下过一个危险结论就不自动收回；
    3. MCP 官方注解：`readOnlyHint` → read；`destructiveHint` → dangerous；
    4. 兜底：沿用能力级 risk_level（与细化前行为完全一致，保守）。

    注解缺失时**不猜**（MCP 规范里 `destructiveHint` 缺省语义是 true，若照此
    推导会把所有 Server 都变成危险，故只认显式 true）。
    """
    override = (declared or {}).get(tool_name)
    if isinstance(override, str) and override in VALID_RISK_LEVELS:
        return override
    if cap_risk == "dangerous":
        return "dangerous"
    if read_only_hint:
        return "read"
    if destructive_hint:
        return "dangerous"
    return cap_risk if cap_risk in VALID_RISK_LEVELS else "read"


async def expand_capability(cap: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """单个 capability 展开为可装配工具条目。

    - tool：payload.schema 即 OpenAI 签名（builtin 占位工具同结构）
    - mcp/plugin：展开连接池中该 Server 的启用工具，暴露名 mcp__{server}__{tool}
      （唯一化，避免与 builtin 或其他 Server 重名）；风险级按工具细化（见
      `resolve_tool_risk`），不再整包沿用能力级
    - skill：M4 渐进披露（load_skill 元工具），此处不展开
    """
    out: list[dict[str, Any]] = []
    payload = cap.get("payload") or {}
    risk = cap.get("risk_level") or "read"
    declared = payload.get("tool_risk_levels")
    declared = declared if isinstance(declared, dict) else {}
    if cap["type"] == "tool":
        # P0-4：平台持有等待后，被取代的轮询类工具不再暴露给模型（模型零轮询）。
        # 放在本函数（两条装配入口的唯一汇聚点）保证 assemble_tools 与
        # search_more_tools 行为一致，不会从检索后门再漏进来。
        if await_policy.is_model_hidden_builtin(payload.get("builtin")):
            return out
        schema = payload.get("schema")
        if schema:
            out.append(
                {
                    "name": cap["name"],
                    "schema": schema,
                    "kind": "tool",
                    "capability_id": cap["id"],
                    "risk_level": risk,
                    "source": source,
                    "builtin": payload.get("builtin"),
                }
            )
    elif cap["type"] in ("mcp", "plugin"):
        from app.modules.capabilities.mcp_client import mcp_pool

        cap_id = UUID(cap["id"])
        for t in await mcp_pool.list_enabled_tools():
            if t["capability_id"] != cap_id:
                continue
            exposed = f"mcp__{cap['name']}__{t['tool_name']}"
            out.append(
                {
                    "name": exposed,
                    "tool_name": t["tool_name"],
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": exposed,
                            "description": t["description"] or exposed,
                            "parameters": t["input_schema"],
                        },
                    },
                    "kind": "mcp",
                    "capability_id": cap["id"],
                    "risk_level": resolve_tool_risk(
                        cap_risk=risk,
                        tool_name=t["tool_name"],
                        declared=declared,
                        read_only_hint=bool(t.get("read_only_hint")),
                        destructive_hint=bool(t.get("destructive_hint")),
                    ),
                    "source": source,
                }
            )
    return out


def take_overflow_payload(cache: dict[str, Any]) -> dict[str, Any] | None:
    """容量不足事件的 at-most-once 取值器（纯函数，便于单测）。

    首次调用返回 payload 并打标；此后返回 None——同一 run 内装配只发生一次，
    但 `_pre_assemble` 与 `context_assembly` 共用同一 cache，没有这道闸会重复报。
    """
    plan = (cache or {}).get("tool_plan") or {}
    if not plan.get("overflow") or plan.get("reported"):
        return None
    plan["reported"] = True
    return {
        "reason": plan.get("reason"),
        "tool_budget": plan.get("tool_budget"),
        "hard_limit": plan.get("hard_limit"),
        "required_count": len(plan.get("required") or []),
        "kept": plan.get("kept") or [],
        "dropped": plan.get("dropped") or [],
    }


async def count_capability_tools(caps: list[dict[str, Any]]) -> int:
    """展开后的可装配工具数（Worker 发布前校验用，方案 §4 P0-2）。

    mcp/plugin 展开成连接池里该 Server 的启用工具（与 runtime 装配同一套口径），
    skill 走渐进披露不计入。按暴露名去重，避免同名工具被重复计数。
    """
    seen: set[str] = set()
    for cap in caps:
        if cap.get("type") == "skill":
            continue
        for item in await expand_capability(cap, "worker"):
            seen.add(item["name"])
    return len(seen)


def partition_tools(
    required: list[dict[str, Any]],
    shared: list[dict[str, Any]],
    *,
    tool_budget: int,
    hard_limit: int = MAX_TOOLS_HARD,
) -> dict[str, Any]:
    """工具分区装配（纯函数，方案 §4 P0-2）：

    `必得集(全量保留) + 共享区[:max(0, tool_budget - len(必得集))]`

    - 必得集**永不切片**：容量不足时保留全部必得集并置 `overflow`（可观测），
      绝不静默截断成"看起来正常"的工具列表；
    - 必得集超出 `hard_limit` 置 `hard_exceeded`（由调用方决定失败策略）；
    - 共享区按传入顺序（即分层距离序）取剩余名额，超出者进 `dropped`。
    """
    core = [t["name"] for t in required]
    room = max(0, tool_budget - len(core))
    kept_items = shared[:room]
    dropped = [t["name"] for t in shared[room:]]
    kept = [t["name"] for t in kept_items]
    overflow = len(core) > tool_budget or bool(dropped)
    return {
        "tools": list(required) + list(kept_items),
        "candidates": core + [t["name"] for t in shared],
        "required": core,
        "kept": kept,
        "dropped": dropped,
        "tool_budget": tool_budget,
        "hard_limit": hard_limit,
        "overflow": overflow,
        "hard_exceeded": len(core) > hard_limit,
        "reason": "tool_budget_insufficient" if overflow else None,
    }


async def assemble_tools(
    query: str,
    agent_id: str,
    tool_budget: int,
    task_id: str | None = None,
    *,
    enforce_hard: bool = True,
) -> dict[str, Any]:
    """工具描述区装配：必得集（pinned + 主任务域）全量保留 + 共享区语义 Top-K + 元工具。

    返回 capability_cache（{"tools": [...], "skills": [...], "tool_plan": {...}}），条目含
    name/schema/kind/capability_id/risk_level/source——tools 节点按 kind 分派执行通道。
    source 记录装配来源（pinned / task_domain / task_common / global），
    使「这个能力属于哪个主任务」在运行期可溯源（能力归属软约束，ADR-28）。
    skills 为语义命中的 SKILL.md 全文列表，供 context_assembly 注入参考。
    tool_plan 是装配全貌（候选/必得/保留/丢弃），供 `capability_overflow` 事件与
    `GET /capabilities/visibility` 自检接口复用。
    enforce_hard=False 供自检接口使用：超硬上限不抛错，只把事实报出来给人看。
    """
    from app.modules.discovery.retriever import retrieve_capabilities

    res = await retrieve_capabilities(query, agent_id, task_id=task_id)
    required: list[dict[str, Any]] = []
    shared: list[dict[str, Any]] = []
    skills: list[str] = []  # 命中 skill 的 SKILL.md 全文
    seen_caps: set[str] = set()
    seen_names: set[str] = set()

    async def _add(caps: list[dict[str, Any]], source: str, sink: list[dict[str, Any]]) -> None:
        for cap in caps:
            if cap["id"] in seen_caps:
                continue
            seen_caps.add(cap["id"])
            # skill 不展开为工具，直接收集 SKILL.md 全文供注入
            if cap.get("type") == "skill":
                md = str((cap.get("payload") or {}).get("skill_md") or "")
                if md.strip():
                    skills.append(md)
                continue
            for item in await expand_capability(cap, cap.get("scope") or source):
                if item["name"] not in seen_names:
                    seen_names.add(item["name"])
                    sink.append(item)

    # 必得集先展开：共享区里重复出现的同名/同能力不再占名额
    await _add(res.get("required") or res["pinned"], "pinned", required)
    await _add(res["semantic"], "semantic", shared)
    plan = partition_tools(required, shared, tool_budget=tool_budget)
    plan["meta"] = [t["name"] for t in _meta_tools()]
    if plan["hard_exceeded"] and enforce_hard:
        raise ToolCapacityExceededError(len(plan["required"]), plan["hard_limit"], plan["required"])
    if plan["overflow"]:
        logger.warning(
            "工具容量不足：必得集 %d / 预算 %d / 丢弃 %d 个候选（agent=%s task=%s）",
            len(plan["required"]),
            tool_budget,
            len(plan["dropped"]),
            agent_id,
            task_id,
        )
    return {"tools": plan["tools"] + _meta_tools(), "skills": skills, "tool_plan": plan}


def merge_tools_cache(cache: dict[str, Any], new_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """把新增工具并入缓存（按 name 去重、保持既有顺序）。

    P1-7：同一轮内可能连续多次 `search_more_tools`，增量语义让调用方按累计缓存
    合并写回，避免"后一次结果覆盖前一次"。
    """
    tools = list((cache or {}).get("tools") or [])
    seen = {t["name"] for t in tools}
    for item in new_tools:
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        tools.append(item)
    return {**(cache or {}), "tools": tools}


async def run_search_more_tools(
    args: dict[str, Any], agent_id: str, cache: dict[str, Any], task_id: str | None = None
) -> tuple[str, dict[str, Any]]:
    """元工具执行：检索 → 文本报告 + **新增**工具（增量，P1-7）。

    返回 `(给模型看的观察文本, {"new_tools": [...]})`——增量而非整体缓存，
    调用方用 `merge_tools_cache` 按累计结果合并（同轮多次搜索不互相覆盖）。
    """
    from app.modules.discovery.retriever import retrieve_capabilities

    query = str(args.get("query") or "")
    res = await retrieve_capabilities(query, agent_id, task_id=task_id)
    existing = list((cache or {}).get("tools") or [])
    seen = {t["name"] for t in existing}
    new_tools: list[dict[str, Any]] = []
    lines: list[str] = []
    for cap in res["semantic"]:
        for item in await expand_capability(cap, cap.get("scope") or "search"):
            if item["name"] in seen:
                continue
            seen.add(item["name"])
            new_tools.append(item)
            desc = str(item["schema"]["function"].get("description") or "")[:80]
            lines.append(f"- {item['name']}：{desc}")
    if not new_tools:
        return "未检索到新的可用工具。", {"new_tools": []}
    return (
        "检索到以下工具，已加入可用工具列表（下一轮可直接调用）：\n" + "\n".join(lines),
        {"new_tools": new_tools},
    )


# ---------- L1/L2 两级压缩 ----------


def _content_text(content: Any) -> str:
    """消息 content → 纯文本（多模态列表取 text 块，图片折叠占位）。

    压缩链路专用：图片 base64 绝不能进 token 估算（否则单图即触发阈值），
    也不能整段 str(list) 进 L2 摘要输入。
    """
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


def _est_tokens(messages: list[AnyMessage]) -> int:
    """粗估 token（中文 ~1.5 字/token，取保守 chars/3）。"""
    return sum(len(_content_text(getattr(m, "content", ""))) for m in messages) // 3


def local_view(
    messages: list[AnyMessage], ops: list[AnyMessage | RemoveMessage]
) -> list[AnyMessage]:
    """在内存中应用 merge 操作（同 id 替换 / RemoveMessage 删除 / 追加）。

    与 LangGraph add_messages 归约语义一致，供 agent 节点在压缩后构建本轮
    LLM 调用的消息视图（checkpoint 状态由返回的 ops 归约更新）。
    """
    orig_ids = {m.id for m in messages if m.id}
    replaced = {
        m.id: m for m in ops if not isinstance(m, RemoveMessage) and m.id and m.id in orig_ids
    }
    removed = {m.id for m in ops if isinstance(m, RemoveMessage)}
    appended = [
        m for m in ops if not isinstance(m, RemoveMessage) and not (m.id and m.id in orig_ids)
    ]
    out = [replaced.get(m.id or "", m) for m in messages if m.id not in removed]
    out.extend(appended)
    return out


async def compact_messages(
    llm: Any,
    messages: list[AnyMessage],
    emit: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> list[AnyMessage | RemoveMessage] | None:
    """上下文估算用量 ≥ 阈值时执行 L1→L2 压缩。

    返回需 merge 进 state 的操作列表（RemoveMessage / 同 id 替换消息 / 摘要
    插入），无需压缩返回 None。L2 摘要失败退回 L1 结果，不阻断主链路。
    """
    if _est_tokens(messages) < settings.context_compact_threshold:
        return None

    # L1：最旧工具观察折叠为一行摘要（保留最近 COMPACT_L1_KEEP_RECENT 条原文）
    tool_msgs = [m for m in messages if m.type == "tool"]
    old_tools = (
        tool_msgs[:-COMPACT_L1_KEEP_RECENT] if len(tool_msgs) > COMPACT_L1_KEEP_RECENT else []
    )
    ops: list[AnyMessage | RemoveMessage] = []
    for m in old_tools:
        # 引用行原样保留（P0-5）：折叠只压正文，产物仍可凭 id 找回完整内容
        brief = artifacts.fold_brief(str(m.content), keep_chars=COMPACT_L1_KEEP_CHARS)
        ops.append(
            ToolMessage(
                content=f"[L1压缩·工具观察折叠] {brief}",
                tool_call_id=str(getattr(m, "tool_call_id", "")),
                id=m.id,
            )
        )
    view = local_view(messages, ops)
    if (
        _est_tokens(view) < settings.context_compact_threshold
        or len(view) <= COMPACT_L2_KEEP_TAIL + 1
    ):
        if ops:
            await emit("context_compacted", {"level": "L1", "folded": len(old_tools)})
            return ops
        return None  # 无可折叠项且未达 L2 条件，放行本轮

    # L2：全量历史摘要重启（保留首尾，中间压缩成段）
    middle, tail = view[1:-COMPACT_L2_KEEP_TAIL], view[-COMPACT_L2_KEEP_TAIL:]
    summary = ""
    try:
        resp = await llm.ainvoke(
            [
                SystemMessage(
                    content="把以下对话历史压缩为一段执行摘要，保留任务目标、关键事实、"
                    "已完成的动作与结论。直接输出摘要正文，不要解释。"
                ),
                HumanMessage(content="\n\n".join(_content_text(m.content) for m in middle)[:12000]),
            ]
        )
        summary = str(resp.content).strip()
    except Exception as e:  # noqa: BLE001 —— 摘要失败退回 L1，不阻断主链路
        logger.warning("L2 摘要失败，退回 L1: %s", e)
    if not summary:
        if ops:
            await emit("context_compacted", {"level": "L1", "folded": len(old_tools)})
        return ops or None

    # 先删后加：移除首条（原始任务）之外全部消息，再回放 摘要 + 尾部
    # （add_messages 顺序归约 → 最终顺序 head, 摘要, tail）
    l2_ops: list[AnyMessage | RemoveMessage] = [RemoveMessage(id=m.id) for m in view[1:] if m.id]
    l2_ops.append(HumanMessage(content=f"[L2压缩·历史摘要] {summary}"))
    l2_ops.extend(tail)
    await emit(
        "context_compacted",
        {"level": "L2", "removed": len(middle), "summary_chars": len(summary)},
    )
    return l2_ops
