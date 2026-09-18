"""skills_forge 服务：复盘 → SKILL.md 草稿 → 去重 → 落 skill_proposals。

流程（模块详细设计 §1.4）：
- review_run：读 run + run_events 提炼执行轨迹 → LLM 三问（方法可复用吗？
  已有 Skill 覆盖吗？写成 SKILL.md 草稿）→ 与现有技能库语义比对：
  相似度 > 0.9 转修订建议（similar_to 指向已有 capability），否则新草稿。
- 触发条件由调用方把关：done 且 iterations ≥ 3（成功复盘），
  或确认卡片改参/驳回（纠错沉淀）。
- 异步发起，不阻塞结果返回（失败只记日志）。
"""

import logging
import uuid
from typing import Any

from langchain_core.messages import HumanMessage
from sqlalchemy import select

from app.core.db import session_factory
from app.modules.capabilities.models import Capability
from app.modules.runs.models import Run, RunEvent
from app.modules.skills_forge.models import SkillProposal

logger = logging.getLogger(__name__)

MIN_ITERATIONS = 3  # 迭代数 ≥ 3 才值得复盘（太简单不沉淀）
SIMILAR_THRESHOLD = 0.9  # 与已有技能余弦相似度超过此值 → 转修订建议


async def _get_chat_llm() -> Any | None:
    """取第一个 enabled 的 llm provider（与 memory 整理 Job 同思路）。"""
    from app.modules.models_module.models import ModelProvider
    from app.modules.models_module.provider import decrypt_secret, get_chat_model

    async with session_factory() as db:
        prov = await db.scalar(
            select(ModelProvider).where(
                ModelProvider.kind == "llm", ModelProvider.status == "enabled"
            )
        )
        if prov is None:
            return None
        api_key = decrypt_secret(prov.api_key_encrypted) if prov.api_key_encrypted else None
        return get_chat_model(prov, api_key)


async def _collect_trace(run_id: str) -> tuple[str, str]:
    """执行轨迹摘要：run 输入/结果 + 关键事件（计划/工具调用/确认）。"""
    async with session_factory() as db:
        run = await db.get(Run, uuid.UUID(run_id))
        if run is None:
            return "", ""
        task_text = str((run.input or {}).get("text", ""))[:500]
        result_text = str((run.result or {}).get("text", ""))[:300]
        events = (
            (
                await db.execute(
                    select(RunEvent)
                    .where(
                        RunEvent.run_id == run.id,
                        RunEvent.event_type.in_(
                            ("plan_updated", "tool_call", "tool_result", "thought")
                        ),
                    )
                    .order_by(RunEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    lines: list[str] = []
    for ev in events:
        p = ev.payload or {}
        if ev.event_type == "plan_updated":
            items = p.get("items") or []
            lines.append("计划: " + "; ".join(str(i.get("text", ""))[:80] for i in items[:5]))
        elif ev.event_type == "tool_call":
            lines.append(f"调用工具 {p.get('name')}: {str(p.get('args'))[:120]}")
        elif ev.event_type == "tool_result":
            # P0-3 起 payload 为 {tool, ok, elapsed_ms, result}；旧行是 {name, content}
            name = p.get("tool") or p.get("name")
            body = p.get("result") if p.get("result") is not None else p.get("content")
            mark = "" if p.get("ok", True) else "[失败]"
            lines.append(f"工具结果{mark}({name}): {str(body)[:120]}")
        elif ev.event_type == "thought":
            lines.append(f"思考: {str(p.get('text'))[:100]}")
    trace = "\n".join(lines)[:8000]
    return f"任务: {task_text}\n结果: {result_text}", trace


_PROMPT = """你是 Agent 平台的技能复盘器。以下是刚完成的一次任务执行记录。

任务与结果：
{summary}

执行轨迹（事件提炼）：
{trace}

请复盘并回答三问，输出格式（严格遵守）：

===REUSABLE===
（第一问：这次任务的方法是否可复用？答"是"或"否"并给一句理由。
单次性的、无固定方法的、纯闲聊的任务答"否"。）

===COVERED===
（第二问：以你的判断，现有通用工具/知识库是否已能覆盖此类任务？
答"是"或"否"并给一句理由。已覆盖则不沉淀。）

===DRAFT===
（第三问：若前两问分别是"是/否"，写一份 SKILL.md 草稿，格式：
---
name: kebab-case-技能名
description: 一句话描述什么场景用这个技能
---
# 步骤
1. ...
# 注意事项
- ...
若不满足沉淀条件，此段只写"无"。）"""


def _parse_reply(reply: str) -> tuple[bool, bool, str]:
    """解析三问输出 → (reusable, not_covered, draft_md)。"""
    text = reply.strip()
    reusable = "是" in _section(text, "REUSABLE")[:20]
    not_covered = "否" in _section(text, "COVERED")[:20]
    draft = _section(text, "DRAFT")
    return reusable, not_covered, draft


def _section(text: str, marker: str) -> str:
    if f"==={marker}===" not in text:
        return ""
    segs = text.split(f"==={marker}===", 1)[1].split("===")
    return segs[0].strip()


async def _find_similar(draft_md: str) -> Capability | None:
    """与现有 skill 类 capability 语义比对：余弦相似度 > 阈值返回已有技能。"""
    from app.modules.discovery.retriever import _embed_query

    vec = await _embed_query(draft_md[:2000])
    if vec is None:
        return None
    async with session_factory() as db:
        cap = (
            await db.execute(
                select(Capability)
                .where(
                    Capability.type == "skill",
                    Capability.embedding.isnot(None),
                    Capability.embedding.cosine_distance(vec) < (1 - SIMILAR_THRESHOLD),
                )
                .order_by(Capability.embedding.cosine_distance(vec))
                .limit(1)
            )
        ).scalar_one_or_none()
        return cap


async def review_run(run_id: str, trigger: str = "success") -> dict[str, Any] | None:
    """复盘一次 run：三问 LLM → 去重 → skill_proposals 落库。

    trigger: success（成功复盘）/ correction（纠错沉淀，草稿侧重注意事项区）。
    """
    summary, trace = await _collect_trace(run_id)
    if not summary:
        return None
    llm = await _get_chat_llm()
    if llm is None:
        logger.info("skills_forge: 无可用 llm provider，跳过 run %s", run_id)
        return None
    try:
        prompt = _PROMPT.format(summary=summary, trace=trace)
        if trigger == "correction":
            # 纠错沉淀：用户驳回过计划，草稿侧重把"别这么做"写进注意事项区
            prompt += (
                "\n\n（补充背景：用户驳回了这次任务的执行计划。"
                "草稿的 # 注意事项 区必须首条写明此次被驳回的原因与应避免的做法。）"
            )
        reply = await llm.ainvoke([HumanMessage(content=prompt)])
    except Exception:  # noqa: BLE001 复盘失败不影响主链路
        logger.exception("skills_forge: run %s 复盘 LLM 调用失败", run_id)
        return None

    reusable, not_covered, draft_md = _parse_reply(str(reply.content))
    if trigger == "success" and not (reusable and not_covered):
        return {"skipped": "方法不可复用或已有能力覆盖"}
    if draft_md in ("", "无") or "name:" not in draft_md:
        return {"skipped": "LLM 未产出有效草稿"}

    similar = await _find_similar(draft_md)
    async with session_factory() as db:
        # 同 run 幂等：已有未审草稿不重复生成
        existing = (
            await db.execute(
                select(SkillProposal).where(
                    SkillProposal.run_id == uuid.UUID(run_id),
                    SkillProposal.status == "proposed",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"skipped": "该 run 已有未审草稿", "proposal_id": str(existing.id)}
        proposal = SkillProposal(
            run_id=uuid.UUID(run_id),
            trigger=trigger,
            draft_md=draft_md,
            similar_to_capability_id=similar.id if similar else None,
        )
        db.add(proposal)
        await db.commit()
        logger.info(
            "skills_forge: run %s 产出草稿 %s（%s）",
            run_id,
            proposal.id,
            "修订建议" if similar else "新技能",
        )
        return {
            "proposal_id": str(proposal.id),
            "trigger": trigger,
            "similar_to": str(similar.id) if similar else None,
        }


async def approve_proposal(proposal_id: str, review_note: str | None = None) -> dict[str, Any]:
    """批准草稿 → 转 capabilities(type=skill) → indexer 进检索池。"""
    from app.modules.capabilities.models import Capability
    from app.modules.capabilities.service import index_capability, validate_payload

    async with session_factory() as db:
        proposal = await db.get(SkillProposal, uuid.UUID(proposal_id))
        if proposal is None:
            return {"error": "草稿不存在"}
        if proposal.status != "proposed":
            return {"error": f"草稿已审（{proposal.status}）"}

        header = _parse_skill_header(proposal.draft_md)
        if header is None:
            return {"error": "草稿 yaml 头缺失 name/description，无法入库"}

        payload = {"skill_md": proposal.draft_md}
        err = validate_payload("skill", payload)
        if err:
            return {"error": f"payload 非法：{err}"}

        name = str(header["name"])
        if proposal.similar_to_capability_id is not None:
            # 修订建议：覆盖已有技能的 payload（版本 +0.1）
            cap = await db.get(Capability, proposal.similar_to_capability_id)
            if cap is not None:
                cap.payload = payload
                cap.description = str(header["description"])
                major, _, minor = str(cap.version or "1.0").partition(".")
                cap.version = f"{major}.{int(minor or 0) + 1}"
            else:
                cap = Capability(
                    type="skill",
                    category="internal",
                    name=name,
                    description=str(header["description"]),
                    payload=payload,
                )
                db.add(cap)
        else:
            # 新技能：同名则升级为覆盖，否则创建
            cap = (
                await db.execute(select(Capability).where(Capability.name == name))
            ).scalar_one_or_none()
            if cap is None:
                cap = Capability(
                    type="skill",
                    category="internal",
                    name=name,
                    description=str(header["description"]),
                    version="1.0",
                    risk_level="read",
                    payload=payload,
                )
                db.add(cap)
            else:
                cap.payload = payload
                cap.description = str(header["description"])

        proposal.status = "approved"
        proposal.review_note = review_note
        from datetime import UTC, datetime

        proposal.reviewed_at = datetime.now(UTC)
        await db.flush()
        assert cap is not None
        await index_capability(db, cap)  # 语义向量 → 检索池
        await db.commit()
        return {
            "capability_id": str(cap.id),
            "name": cap.name,
            "version": cap.version,
        }


async def reject_proposal(proposal_id: str, review_note: str | None = None) -> dict[str, Any]:
    """驳回草稿：状态落 rejected，不进检索池。"""
    async with session_factory() as db:
        proposal = await db.get(SkillProposal, uuid.UUID(proposal_id))
        if proposal is None:
            return {"error": "草稿不存在"}
        if proposal.status != "proposed":
            return {"error": f"草稿已审（{proposal.status}）"}
        proposal.status = "rejected"
        proposal.review_note = review_note
        from datetime import UTC, datetime

        proposal.reviewed_at = datetime.now(UTC)
        await db.commit()
        return {"proposal_id": proposal_id, "status": "rejected"}


def _parse_skill_header(skill_md: str) -> dict[str, Any] | None:
    """解析 SKILL.md 的 --- 包围 yaml 头（name/description 必填）。"""
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
