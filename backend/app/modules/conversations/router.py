"""conversations 路由：会话管理 + 发消息（异步投递，接口永远快）。

发消息不同步执行（模块详细设计 §2.2）：
落 message → 创建 run(pending) → 投 inbox_events + NOTIFY → 返回 run_id。
"""

import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.agents.models import Agent
from app.modules.auth.deps import get_current_user
from app.modules.conversations.models import Conversation, Message
from app.modules.conversations.schemas import (
    ConversationCreateIn,
    ConversationOut,
    MessageIn,
    MessageOut,
    SendMessageOut,
)
from app.modules.runs.models import Run

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(db: AsyncSession = Depends(get_db)) -> list[Conversation]:
    stmt = select(Conversation).order_by(Conversation.last_message_at.desc().nullslast())
    return list((await db.scalars(stmt)).all())


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateIn, db: AsyncSession = Depends(get_db)
) -> Conversation:
    if body.agent_id:
        agent = await db.get(Agent, body.agent_id)
        if agent is None or agent.status != "enabled":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent 不存在或已停用")
    else:
        agent = await db.scalar(
            select(Agent).where(Agent.is_default.is_(True), Agent.status == "enabled")
        )
        if agent is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "无可用 Agent，请先创建")
    conv = Conversation(agent_id=agent.id, title=body.title)
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: UUID,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


@router.post(
    "/{conversation_id}/messages",
    response_model=SendMessageOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: UUID,
    body: MessageIn,
    db: AsyncSession = Depends(get_db),
) -> SendMessageOut:
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if conv.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "会话已关闭，请新开会话")
    agent = await db.get(Agent, conv.agent_id)
    if agent is None or agent.status != "enabled":
        raise HTTPException(status.HTTP_409_CONFLICT, "会话绑定的 Agent 不可用")

    # 1) 落用户消息
    db.add(Message(conversation_id=conv.id, role="user", content={"text": body.text}))
    conv.message_count = (conv.message_count or 0) + 1
    conv.last_message_at = datetime.now(UTC)

    # 2) 创建 run：预算快照创建时固化（数据库设计 §2.3）
    run = Run(
        conversation_id=conv.id,
        agent_id=conv.agent_id,
        trigger="manual",
        input={"text": body.text},
        budget={
            "tool_budget": agent.tool_budget,
            "max_iterations": agent.max_iterations,
            "max_tokens_per_run": agent.max_tokens_per_run,
            "timeout_seconds": agent.timeout_seconds,
        },
    )
    db.add(run)
    await db.flush()  # 拿 run.id

    # 3) 投 inbox + NOTIFY（唤醒引擎 worker）
    await db.execute(
        text(
            "INSERT INTO inbox_events (event_type, target_run_id, payload, status) "
            "VALUES ('user_input', :rid, CAST(:p AS jsonb), 'new')"
        ),
        {"rid": run.id, "p": json.dumps({"run_id": str(run.id)}, ensure_ascii=False)},
    )
    await db.execute(text("SELECT pg_notify('inbox_events', :rid)"), {"rid": str(run.id)})
    await db.commit()
    return SendMessageOut(conversation_id=conv.id, run_id=run.id)
