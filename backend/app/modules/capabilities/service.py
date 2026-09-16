"""capabilities 服务：CRUD + payload 校验 + 注册流程 + indexer（模块详细设计 §1.3）。

注册流程：提交 → 规范校验 → 冒烟（smoke.run_smoke_test）→ 通过才 enabled
→ indexer 写 embedding → pg_notify('capability_changed') 热广播。

builtin seed（M2-2c）保留：零配置冒烟链路的一部分，幂等按 name upsert。
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_factory
from app.modules.capabilities.models import (
    Capability,
    CapabilityBinding,
    CapabilityTool,
)
from app.modules.capabilities.schemas import (
    CapabilityCreateIn,
    CapabilitySmokeReport,
    CapabilityUpdateIn,
)
from app.modules.engine.tools_builtin import BUILTIN_TOOLS, builtin_tool_schema

# ---------- builtin seed（M2-2c） ----------

# builtin 占位工具清单（name → 描述 / 风险级 / 参数 schema）
BUILTIN_SEED: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "占位工具：原样返回输入文本（验证工具调用链路）",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要原样返回的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "dangerous_demo",
        "description": "占位高危工具：演示确认卡片流程（无真实副作用）",
        "risk_level": "dangerous",
        "params": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "要执行的危险动作名"},
                "target": {"type": "string", "description": "目标对象"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "search_knowledge",
        "description": "知识库语义检索：用自然语言查询平台知识库（已上传并解析完成的文档），"
        "返回最相关的文本片段（含文档标题/目录/标题路径）。可指定目录范围。",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索问题/关键词（自然语言）",
                },
                "folders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选，目录范围（物化路径前缀，如 [\"/产品知识\"]）；缺省全库",
                },
                "k": {"type": "integer", "description": "返回条数，缺省 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_legal_crawl",
        "description": "触发法规爬取管道（后台异步执行，立即返回）：爬取 EU TRIS/德国联邦议院/"
        "英国议会/GOV.UK/legislation.gov.uk 的法规动态，LLM 提取生成独立报告与当日汇总。"
        "耗时数分钟到几十分钟，完成后可用 legal_crawl_status 查询产出。",
        "risk_level": "write",
        "params": {
            "type": "object",
            "properties": {
                "skip_llm": {
                    "type": "boolean",
                    "description": "true 则只爬取不做 LLM 提取（更快，默认 false）",
                },
            },
        },
    },
    {
        "name": "legal_crawl_status",
        "description": "查询法规爬虫的最近产出概况：最近几个爬取日的新增条数/文件数/运行状态。",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "查看最近几天（默认 3，最大 10）",
                },
            },
        },
    },
    {
        "name": "probe_url",
        "description": "依赖预检：探测某个服务/API 是否可达（GET/HEAD 请求），"
        "返回可达性、状态码、延迟。任务开始前应先检查依赖项，不通则告知用户并停止。",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要探测的服务地址（如 http://localhost:8100/health）",
                },
                "method": {
                    "type": "string",
                    "description": "HTTP 方法 GET 或 HEAD，默认 GET",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时秒数（默认 10，最大 30）",
                },
            },
            "required": ["url"],
        },
    },
    # ---- 采购报价对比 Agent 桥接能力（决策4：外部服务，异步桥接范式，§5）----
    {
        "name": "procurement_trigger",
        "description": "触发采购报价对比 Agent 解析报价单（外部服务异步执行，立即返回 task_id）。"
        "传入报价单文本或文件引用；解析耗时较长，随后用 procurement_status 轮询进度。",
        "risk_level": "write",
        "params": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "报价单文本（与 report_ref 二选一）"},
                "report_ref": {
                    "type": "string",
                    "description": "报价单文件引用/路径（与 text 二选一）",
                },
            },
        },
    },
    {
        "name": "procurement_status",
        "description": "轮询采购报价解析任务的状态与九段流水线进度"
        "（传 procurement_trigger 返回的 task_id）。",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "采购解析任务 id"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "procurement_decisions",
        "description": "拉取采购任务的待决策面（供应商建档/工艺确认/项目绑定/数据纠错），"
        "返回的每个决策面 schema 与交互决策卡/侧边栏对齐，供 request_decision 抛卡使用。",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "采购解析任务 id"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "procurement_submit_decision",
        "description": "把用户在侧边栏处理完的结构化结果写回采购 Agent（落库）。"
        "传决策 id 与回传数据。",
        "risk_level": "write",
        "params": {
            "type": "object",
            "properties": {
                "decision_id": {"type": "string", "description": "待决策面的 id"},
                "data": {"type": "object", "description": "侧边栏结构化回传的数据（选/删/改结果）"},
            },
            "required": ["decision_id"],
        },
    },
    {
        "name": "procurement_report",
        "description": "拉取采购报价对比报告与 AI 综合分析（产出可经知识库管道入库供跨任务检索）。",
        "risk_level": "read",
        "params": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "采购解析任务 id"}},
            "required": ["task_id"],
        },
    },
]


async def seed_builtin_capabilities() -> None:
    async with session_factory() as db:
        for item in BUILTIN_SEED:
            cap = await db.scalar(select(Capability).where(Capability.name == item["name"]))
            payload = {
                "builtin": item["name"],
                "schema": builtin_tool_schema(item["name"], item["description"], item["params"]),
            }
            if cap is None:
                cap = Capability(
                    type="tool",
                    category="builtin",
                    name=item["name"],
                    description=item["description"],
                    risk_level=item["risk_level"],
                    payload=payload,
                    health_status="ok",
                )
                db.add(cap)
            else:
                cap.description = item["description"]
                cap.risk_level = item["risk_level"]
                cap.payload = payload
                cap.health_status = "ok"
            # 语义向量：builtin 工具也要能被 retriever 语义命中（无 provider 时跳过）
            await index_capability(db, cap)
        await db.commit()


# ---------- payload 规范校验 ----------


def _validate_frontend_manifest(frontend: Any) -> str | None:
    """校验 plugin 的前端清单（payload.frontend，设计方案 §3.5）。

    平台中只有 plugin 携带前端页面，由侧边栏渲染。None/缺省 = 无前端（合法，
    纯后端 plugin）；有则必须是 dict 且 mode ∈ {iframe, server_driven}：
    - iframe（推荐外部 plugin）：url 必须 http(s)；sandbox/allowlist_origin 可选
    - server_driven（推荐平台原生）：schema 必须是 dict（JSON UI schema）
    """
    if frontend is None:
        return None
    if not isinstance(frontend, dict):
        return "payload.frontend 必须是对象"
    mode = frontend.get("mode")
    if mode == "iframe":
        url = frontend.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            return "frontend.mode=iframe 需要 frontend.url（http(s)://…）"
        return None
    if mode == "server_driven":
        if not isinstance(frontend.get("schema"), dict):
            return "frontend.mode=server_driven 需要 frontend.schema（JSON UI schema 对象）"
        return None
    return "frontend.mode 必须是 iframe 或 server_driven"


def _validate_transport(payload: dict[str, Any]) -> str | None:
    """校验 mcp/plugin 的后端 transport（stdio/http）+ env。"""
    transport = payload.get("transport")
    if transport not in ("stdio", "http"):
        return "mcp/plugin 类型需要 payload.transport（stdio/http）"
    if transport == "stdio":
        if not isinstance(payload.get("command"), str) or not payload["command"].strip():
            return "stdio 传输需要 payload.command（启动命令）"
        args = payload.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return "payload.args 必须是字符串数组"
    else:
        if not isinstance(payload.get("url"), str) or not payload["url"].startswith("http"):
            return "http 传输需要 payload.url（http(s)://…）"
    env = payload.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        return "payload.env 必须是 {str: str}"
    return None


def validate_payload(type_: str, payload: dict[str, Any]) -> str | None:
    """按 type 校验 payload 结构，返回错误文案（None = 通过）。"""
    if type_ == "tool":
        if payload.get("builtin") in BUILTIN_TOOLS:
            return None
        schema = payload.get("schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return "tool 类型需要 payload.schema（OpenAI 函数签名，type=object）或 payload.builtin"
        return None
    if type_ == "skill":
        skill_md = payload.get("skill_md")
        if not isinstance(skill_md, str) or not skill_md.strip():
            return "skill 类型需要 payload.skill_md（SKILL.md 全文）"
        if _parse_skill_yaml_header(skill_md) is None:
            return "skill_md 的 yaml 头不完整（需要 --- 包围的 name/description）"
        return None
    if type_ == "plugin":
        # plugin 可纯前端（展示类，无后端进程），也可携带后端 transport；
        # 两者至少有其一：有 frontend 则校验清单，有 transport 则校验后端。
        frontend_err = _validate_frontend_manifest(payload.get("frontend"))
        if frontend_err is not None:
            return frontend_err
        has_frontend = isinstance(payload.get("frontend"), dict)
        has_transport = payload.get("transport") in ("stdio", "http")
        if not has_frontend and not has_transport:
            return "plugin 类型需要 payload.frontend（前端清单）或 payload.transport（stdio/http）"
        if has_transport:
            transport_err = _validate_transport(payload)
            if transport_err is not None:
                return transport_err
        return None
    if type_ == "mcp":
        transport_err = _validate_transport(payload)
        if transport_err is not None:
            return transport_err
        return None
    return f"未知能力类型: {type_}"


def _parse_skill_yaml_header(skill_md: str) -> dict[str, Any] | None:
    """解析 SKILL.md 的 --- 包围 yaml 头。仅校验 name/description 存在。"""
    import yaml

    text = skill_md.strip()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        header = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(header, dict) or not header.get("name") or not header.get("description"):
        return None
    return header


# ---------- CRUD ----------


async def get_capability_or_404(db: AsyncSession, cap_id: UUID) -> Capability:
    cap = await db.get(Capability, cap_id)
    if cap is None:
        from fastapi import HTTPException, status

        raise HTTPException(status.HTTP_404_NOT_FOUND, "capability 不存在")
    return cap


def _merge_secret_env(payload: dict[str, Any], secret_env: dict[str, str]) -> dict[str, Any]:
    """敏感 env Fernet 加密后存 payload.secret_env_encrypted（hex）。"""
    from app.modules.models_module.provider import encrypt_secret

    payload = dict(payload)
    enc = {}
    for k, v in secret_env.items():
        token = encrypt_secret(v)
        enc[k] = token.hex()
    payload["secret_env_encrypted"] = enc
    return payload


def strip_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """出参剥离加密内容。"""
    return {k: v for k, v in (payload or {}).items() if k != "secret_env_encrypted"}


def decrypt_secret_env(payload: dict[str, Any]) -> dict[str, str]:
    """运行时解密 payload.secret_env_encrypted → env dict。"""
    from app.modules.models_module.provider import decrypt_secret

    out: dict[str, str] = {}
    for k, hex_token in (payload.get("secret_env_encrypted") or {}).items():
        plain = decrypt_secret(bytes.fromhex(hex_token))
        if plain is not None:
            out[k] = plain
    return out


async def list_capabilities(db: AsyncSession, include_disabled: bool = False) -> list[Capability]:
    stmt = select(Capability)
    if not include_disabled:
        stmt = stmt.where(Capability.enabled.is_(True))  # noqa: E712
    stmt = stmt.order_by(Capability.type, Capability.name)
    return list((await db.scalars(stmt)).all())


async def create_capability(
    db: AsyncSession, body: CapabilityCreateIn
) -> tuple[Capability, CapabilitySmokeReport]:
    """注册：校验 → 落库（enabled=false 待冒烟）→ 冒烟 → 通过才启用 + indexer + 广播。"""
    from fastapi import HTTPException, status

    err = validate_payload(body.type, body.payload)
    if err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, err)
    exists = await db.scalar(select(Capability).where(Capability.name == body.name))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, f"能力名已存在: {body.name}")

    payload = dict(body.payload)
    if body.secret_env:
        payload = _merge_secret_env(payload, body.secret_env)
    cap = Capability(
        type=body.type,
        category=body.category,
        name=body.name,
        description=body.description,
        version=body.version,
        risk_level=body.risk_level,
        payload=payload,
        test_info=body.test_info,
        enabled=False,  # 冒烟通过才启用
    )
    db.add(cap)
    await db.flush()

    # 冒烟（临时连接，不影响连接池）
    from app.modules.capabilities.smoke import run_smoke_test

    report = await run_smoke_test(db, cap)
    if report.passed:
        cap.enabled = True
        await db.flush()
        await index_capability(db, cap)
        await _broadcast_changed(cap.id)
    await db.commit()
    await db.refresh(cap)
    return cap, report


async def update_capability(
    db: AsyncSession, cap: Capability, body: CapabilityUpdateIn
) -> Capability:
    from fastapi import HTTPException, status

    if body.payload is not None:
        err = validate_payload(cap.type, body.payload)
        if err:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, err)
    # 先应用字段修改（冒烟必须用新 payload 跑，否则修配置永远过不了旧配置的坎）
    if body.description is not None:
        cap.description = body.description
    if body.version is not None:
        cap.version = body.version
    if body.risk_level is not None:
        cap.risk_level = body.risk_level
    if body.payload is not None:
        payload = dict(body.payload)
        if body.secret_env:
            payload = _merge_secret_env(payload, body.secret_env)
        elif "secret_env_encrypted" in (cap.payload or {}):
            # 未提交新密钥时保留旧密钥
            payload["secret_env_encrypted"] = cap.payload["secret_env_encrypted"]
        cap.payload = payload
    if body.test_info is not None:
        cap.test_info = body.test_info
    await db.flush()
    if body.enabled is True and not cap.enabled:
        # 重新启用 = 重跑冒烟（此时已是新 payload）
        from app.modules.capabilities.smoke import run_smoke_test

        report = await run_smoke_test(db, cap)
        if not report.passed:
            # 先提交保留用户的配置修改，再拒绝启用（否则修的配置会被 409 回滚丢掉）
            await db.commit()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"冒烟未通过，不能启用（配置修改已保留）: {report.summary}",
            )
        cap.enabled = True
        await db.flush()
    elif body.enabled is not None:
        cap.enabled = body.enabled
        await db.flush()
    # 描述/payload 变更 → 重建索引 + 热广播
    if body.description is not None or body.payload is not None:
        await index_capability(db, cap)
    if body.description is not None or body.payload is not None or body.enabled is not None:
        await _broadcast_changed(cap.id)
    await db.commit()
    await db.refresh(cap)
    return cap


async def delete_capability(db: AsyncSession, cap: Capability) -> None:
    await db.delete(cap)
    await db.commit()
    await _broadcast_changed(cap.id)


# ---------- 工具级开关 ----------


async def list_tools(db: AsyncSession, cap: Capability) -> list[CapabilityTool]:
    stmt = (
        select(CapabilityTool)
        .where(CapabilityTool.capability_id == cap.id)
        .order_by(CapabilityTool.tool_name)
    )
    return list((await db.scalars(stmt)).all())


async def set_tool_enabled(
    db: AsyncSession, cap: Capability, tool_name: str, enabled: bool
) -> CapabilityTool:
    from fastapi import HTTPException, status

    tool = await db.scalar(
        select(CapabilityTool).where(
            CapabilityTool.capability_id == cap.id,
            CapabilityTool.tool_name == tool_name,
        )
    )
    if tool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"工具不存在: {tool_name}")
    tool.enabled = enabled
    await db.commit()
    await db.refresh(tool)
    await _broadcast_changed(cap.id)
    return tool


# ---------- capability_bindings ----------


async def list_bindings(db: AsyncSession, agent_id: UUID) -> list[CapabilityBinding]:
    stmt = (
        select(CapabilityBinding)
        .where(CapabilityBinding.agent_id == agent_id)
        .order_by(CapabilityBinding.created_at)
    )
    return list((await db.scalars(stmt)).all())


async def upsert_binding(
    db: AsyncSession, agent_id: UUID, capability_id: UUID, mode: str
) -> CapabilityBinding:
    from fastapi import HTTPException, status

    cap = await db.get(Capability, capability_id)
    if cap is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "capability 不存在")
    binding = await db.scalar(
        select(CapabilityBinding).where(
            CapabilityBinding.agent_id == agent_id,
            CapabilityBinding.capability_id == capability_id,
        )
    )
    if binding is None:
        binding = CapabilityBinding(agent_id=agent_id, capability_id=capability_id, mode=mode)
        db.add(binding)
    else:
        binding.mode = mode
    await db.commit()
    await db.refresh(binding)
    return binding


async def delete_binding(db: AsyncSession, agent_id: UUID, capability_id: UUID) -> None:
    from fastapi import HTTPException, status

    binding = await db.scalar(
        select(CapabilityBinding).where(
            CapabilityBinding.agent_id == agent_id,
            CapabilityBinding.capability_id == capability_id,
        )
    )
    if binding is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "绑定不存在")
    await db.delete(binding)
    await db.commit()


# ---------- indexer（模块详细设计 §1.2.1） ----------


def _index_text(cap: Capability) -> str:
    """name + description + 签名摘要 → embedding 输入文本。"""
    payload = cap.payload or {}
    signature = ""
    if cap.type == "tool":
        props = (payload.get("schema") or {}).get("properties") or {}
        signature = "参数: " + ", ".join(props.keys())
    elif cap.type == "skill":
        header = _parse_skill_yaml_header(payload.get("skill_md") or "")
        signature = (header or {}).get("description", "") or ""
    elif cap.type in ("mcp", "plugin"):
        endpoint = payload.get("command", payload.get("url", ""))
        signature = f"transport={payload.get('transport')} command={endpoint}"
    return f"{cap.name}：{cap.description}。{signature}"


async def index_capability(db: AsyncSession, cap: Capability) -> bool:
    """写回语义向量。无可用 embedding provider 时跳过（返回 False）。"""
    from app.modules.models_module.models import ModelProvider
    from app.modules.models_module.provider import decrypt_secret, get_embeddings

    prov = await db.scalar(
        select(ModelProvider).where(
            ModelProvider.kind == "embedding", ModelProvider.status == "enabled"
        )
    )
    if prov is None:
        return False
    api_key = decrypt_secret(prov.api_key_encrypted) if prov.api_key_encrypted else None
    embeddings = get_embeddings(prov, api_key)
    vec = await embeddings.aembed_query(_index_text(cap))
    cap.embedding = vec
    cap.embedding_model = prov.model_name
    return True


# ---------- 热注册广播 ----------


async def _broadcast_changed(cap_id: UUID) -> None:
    """pg_notify → engine mcp 池/检索池重建（模块详细设计 §1.3.3，M3-d 消费）。"""
    import asyncpg

    from app.core.config import settings

    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SELECT pg_notify('capability_changed', $1)", str(cap_id))
    finally:
        await conn.close()


def capability_smoke_report_dict(report: CapabilitySmokeReport) -> dict[str, Any]:
    return json.loads(report.model_dump_json())
