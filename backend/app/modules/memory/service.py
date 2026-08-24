"""memory 服务：每日整理 Job + 记忆查询/手改 + 注入读取。

整理 Job（每日 03:00，模块详细设计 §2.6）：
- 读目标日全部 runs + messages → LLM 提炼两份产物：
  1. platform 要点（与用户无关的平台级事实）→ 追加 platform 记忆
  2. daily 要点（用户偏好/纠正/事实）→ 当日 daily 文件
- 新内容 embedding 入库；顺带 close 旧会话（会话切割）
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.db import session_factory
from app.modules.conversations.models import Conversation, Message
from app.modules.memory.models import MemoryFile
from app.modules.runs.models import Run

logger = logging.getLogger(__name__)

TOKENS_PER_CHAR = 2  # 与 chunker 一致的中文粗估（chars/2）


def _est_tokens(text: str) -> int:
    return max(1, len(text) // TOKENS_PER_CHAR)


async def _get_chat_llm() -> Any | None:
    """取第一个 enabled 的 llm provider 构造 LLM（整理 Job 无特定 Agent 归属）。"""
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


async def _collect_material(target: date) -> tuple[str, list[str]]:
    """收集目标日素材：runs 概要 + messages 文本。返回 (素材文本, run_ids)。"""
    tz = UTC
    start = datetime.combine(target, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    parts: list[str] = []
    run_ids: list[str] = []
    async with session_factory() as db:
        runs = (
            (
                await db.execute(
                    select(Run)
                    .where(Run.created_at >= start, Run.created_at < end)
                    .order_by(Run.created_at)
                )
            )
            .scalars().all()
        )
        for r in runs:
            run_ids.append(str(r.id))
            parts.append(
                f"[run {r.trigger}/{r.status}] input={(r.input or {}).get('text', '')[:200]}"
            )
        msgs = (
            (
                await db.execute(
                    select(Message, Conversation)
                    .join(Conversation, Message.conversation_id == Conversation.id)
                    .where(Message.created_at >= start, Message.created_at < end)
                    .order_by(Message.created_at)
                )
            )
            .all()
        )
        for m, _conv in msgs:
            text = (m.content or {}).get("text") or ""
            if text:
                parts.append(f"[{m.role}] {text[:300]}")
    return "\n".join(parts), run_ids


_PROMPT = """你是 Agent 平台的记忆整理器。以下是 {target} 当天的任务与对话记录素材。

请提炼为两份 Markdown 记忆，输出格式（严格遵守，两段用 ===PLATFORM=== / ===DAILY=== 分隔符）：

===PLATFORM===
（平台级要点：与用户个人无关、对平台运行有长期价值的事实，如工具故障记录、配置结论、
流程经验。没有则只写"无"。每条一行 "- " 列表项，最多 5 条。）

===DAILY===
（用户当日要点：用户偏好、纠正、个人事实、进行中的事项。没有则只写"无"。
每条一行 "- " 列表项，最多 8 条。）

素材：
{material}"""


def _parse_products(reply: str) -> tuple[str, str]:
    """解析 LLM 双产物输出 → (platform_md, daily_md)。"""
    text = reply.strip()
    platform_md, daily_md = "无", "无"
    if "===PLATFORM===" in text:
        rest = text.split("===PLATFORM===", 1)[1]
        segs = rest.split("===DAILY===")
        platform_md = segs[0].strip() or "无"
        daily_md = segs[1].strip() if len(segs) > 1 and segs[1].strip() else "无"
    elif "===DAILY===" in text:
        daily_md = text.split("===DAILY===", 1)[1].strip() or "无"
    return platform_md, daily_md


async def _embed_memories(items: list[MemoryFile]) -> None:
    """新记忆写语义向量（无 embedding provider 时跳过）。"""
    from app.modules.discovery.retriever import _embed_query  # 复用 provider 查询逻辑

    for item in items:
        vec = await _embed_query(f"{item.title}\n{item.content}")
        if vec is not None:
            item.embedding = vec


async def consolidate_daily(target: date | None = None) -> dict[str, Any]:
    """整理 Job：素材 → LLM 提炼 → platform/daily 双产物落库 + 会话切割。

    幂等：同一天重跑会覆盖当日 daily（platform 追加前会截去上次同源追加段不可行，
    从简：重跑 = 重新整理当日 daily + platform 按日标记段落替换）。
    """
    if target is None:
        target = date.today() - timedelta(days=1)  # 03:00 整理昨天
    result: dict[str, Any] = {
        "date": str(target),
        "runs": 0,
        "daily_written": False,
        "platform_appended": False,
    }
    material, run_ids = await _collect_material(target)
    result["runs"] = len(run_ids)
    if not material.strip():
        result["skipped"] = "当日无 runs/messages 素材"
        await _close_stale_conversations()
        return result

    llm = await _get_chat_llm()
    if llm is None:
        result["skipped"] = "无可用 chat provider"
        return result
    from langchain_core.messages import HumanMessage

    reply = await llm.ainvoke(
        [HumanMessage(content=_PROMPT.format(target=target, material=material[:12000]))]
    )
    platform_md, daily_md = _parse_products(str(reply.content))

    new_items: list[MemoryFile] = []
    async with session_factory() as db:
        # daily：当日一条 upsert
        daily = (
            await db.execute(
                select(MemoryFile).where(MemoryFile.kind == "daily", MemoryFile.date == target)
            )
        ).scalar_one_or_none()
        if daily_md != "无":
            if daily is None:
                daily = MemoryFile(
                    kind="daily", date=target, title=f"{target} 日记忆", content=daily_md
                )
                db.add(daily)
            else:
                daily.content = daily_md
            daily.source_run_ids = run_ids
            daily.token_count = _est_tokens(daily.content)
            new_items.append(daily)
            result["daily_written"] = True

        # platform：追加（带日期段标记，重跑同日替换该段）
        if platform_md != "无":
            plat = (
                await db.execute(
                    select(MemoryFile)
                    .where(MemoryFile.kind == "platform")
                    .order_by(MemoryFile.created_at.desc())
                )
            ).scalar_one_or_none()
            section = f"\n\n## {target}\n{platform_md}"
            if plat is None:
                plat = MemoryFile(
                    kind="platform", date=None, title="平台记忆", content=f"# 平台记忆{section}"
                )
                db.add(plat)
                result["platform_appended"] = True
            else:
                marker = f"## {target}"
                if marker in plat.content:  # 同日重跑：替换该日段落
                    head, _, tail = plat.content.partition(marker)
                    _, _, rest = tail.partition("\n## ")
                    plat.content = head + marker + platform_md + ("\n## " + rest if rest else "")
                else:
                    plat.content += section
                    result["platform_appended"] = True
            plat.source_run_ids = list(set((plat.source_run_ids or []) + run_ids))
            plat.token_count = _est_tokens(plat.content)
            new_items.append(plat)

        # 向量在同一 session 内写（commit 前对象仍 attached，避免跨 session 重挂）
        try:
            await _embed_memories(new_items)
        except Exception:  # noqa: BLE001 向量失败不阻断记忆落库
            logger.exception("记忆 embedding 写入失败（记忆文本照常落库）")
        await db.commit()

    await _close_stale_conversations()
    return result


async def _close_stale_conversations() -> int:
    """会话切割：last_message_at 早于今日 03:00 的 active 会话 → closed。"""
    cutoff = datetime.combine(date.today(), time(3, 0), tzinfo=UTC)
    async with session_factory() as db:
        result = await db.execute(
            update(Conversation)
            .where(Conversation.status == "active", Conversation.last_message_at < cutoff)
            .values(status="closed")
        )
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def get_protected_memories() -> str:
    """注入 assembler protected 区：platform 全文 + 最近 2 天 daily（模块详细设计 §2.6）。"""
    async with session_factory() as db:
        plat = (
            await db.execute(
                select(MemoryFile)
                .where(MemoryFile.kind == "platform")
                .order_by(MemoryFile.created_at.desc())
            )
        ).scalar_one_or_none()
        since = date.today() - timedelta(days=2)
        dailies = (
            (
                await db.execute(
                    select(MemoryFile)
                    .where(MemoryFile.kind == "daily", MemoryFile.date >= since)
                    .order_by(MemoryFile.date.desc())
                )
            )
            .scalars().all()
        )
    parts: list[str] = []
    # platform content 自带 "# 平台记忆" 标题，不重复拼
    if plat and plat.content.strip():
        parts.append(plat.content.strip())
    if dailies:
        blocks = [f"# {d.date} 日记忆\n{d.content}" for d in dailies if d.content.strip()]
        if blocks:
            parts.append("\n\n".join(blocks))
    return "\n\n---\n\n".join(parts)
