"""conversations 路由：会话管理 + 发消息（异步投递，接口永远快）。

发消息不同步执行（模块详细设计 §2.2）：
落 message → 创建 run(pending) → 投 inbox_events + NOTIFY → 返回 run_id。
"""

import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.agents.models import Agent
from app.modules.auth.deps import get_current_user
from app.modules.conversations.models import Conversation, Message
from app.modules.conversations.schemas import (
    ConversationCreateIn,
    ConversationOut,
    ConversationUpdateIn,
    MessageIn,
    MessageOut,
    SendMessageOut,
)
from app.modules.files import service as files_service
from app.modules.files.models import File
from app.modules.models_module.models import ModelProvider
from app.modules.runs.models import Run

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(db: AsyncSession = Depends(get_db)) -> list[Conversation]:
    # 排序键 = 最后消息时间，没有则用创建时间（新建即置顶，无消息空会话不沉底）
    stmt = select(Conversation).order_by(
        func.coalesce(Conversation.last_message_at, Conversation.created_at).desc()
    )
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


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: UUID,
    body: ConversationUpdateIn,
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    """重命名 / 关闭会话（部分更新）。"""
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if body.title is not None:
        conv.title = body.title
    if body.status is not None:
        conv.status = body.status
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID, db: AsyncSession = Depends(get_db)
) -> None:
    """删除会话：消息/任务/事件/计划随 FK CASCADE 全部级联清除。

    活跃任务先投 abort（尽力而为的优雅终止）：引擎若还在执行，后续写库
    会因 run 行已删而 FK violation，被 process_run 异常路径吞掉，无脏数据。
    file_refs.ref_id 无 FK（M1 设计：无引用即孤儿），附件文件本身保留。
    """
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    # 非终态 run：投 abort 控制面事件（与 POST /runs/{id}/abort 同机制）
    active_runs = (
        await db.scalars(
            select(Run).where(
                Run.conversation_id == conv.id,
                Run.status.notin_(("done", "failed", "cancelled", "aborted", "timeout")),
            )
        )
    ).all()
    for run in active_runs:
        await db.execute(
            text(
                "INSERT INTO inbox_events (event_type, target_run_id, payload, status) "
                "VALUES ('abort', :rid, '{}'::jsonb, 'new')"
            ),
            {"rid": run.id},
        )
        await db.execute(text("SELECT pg_notify('inbox_events', :rid)"), {"rid": str(run.id)})
    await db.delete(conv)
    await db.commit()


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: UUID,
    limit: int = 200,
    before_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    """按时间升序返回最近 limit 条；before_id 游标向前翻页（加载更早）。

    注意不能升序 + limit（那取到的是最旧一页，长会话最新消息反而看不到）；
    必须倒序取尾部再反转。返回条数 == limit 时前端可认为还有更早的历史。
    """
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if before_id is not None:
        anchor = await db.get(Message, before_id)
        if anchor is not None:
            stmt = stmt.where(Message.created_at < anchor.created_at)
    stmt = stmt.order_by(Message.created_at.desc()).limit(limit)
    rows = list((await db.scalars(stmt)).all())
    rows.reverse()
    return rows


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

    # 对话内临时换模型：校验可用性，随 run.input 快照固化（数据库设计 §2.3）
    model_override: str | None = None
    if body.model_provider_id is not None:
        provider = await db.get(ModelProvider, body.model_provider_id)
        if provider is None or provider.status != "enabled" or provider.kind != "llm":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "模型不存在或不可用")
        model_override = str(provider.id)

    # 附件校验：批量查 File 记录（已入库即已过白名单/大小限制）
    attachments: list[dict] = []
    has_image = False
    if body.attachment_ids:
        stmt = select(File).where(File.id.in_(body.attachment_ids))
        found = {f.id: f for f in (await db.scalars(stmt)).all()}
        missing = [str(i) for i in body.attachment_ids if i not in found]
        if missing:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"附件不存在：{', '.join(missing)}"
            )
        attachments = [
            {
                "file_id": str(f.id),
                "filename": f.filename,
                "mime": f.mime,
                "size": f.size,
            }
            for f in (found[i] for i in body.attachment_ids)
        ]
        has_image = any((a["mime"] or "").startswith("image/") for a in attachments)

    # 图片外发涉密确认门：带图且生效模型支持视觉 → 图片将上传至模型服务商。
    # 未确认时 428，前端弹窗（含服务商名）确认后携 confirm_upload=true 重发。
    # 文档附件走本地 docling 容器提取，不出本机，无需确认。
    if has_image and not body.confirm_upload:
        effective_pid = model_override or str(agent.model_provider_id or "")
        ep = await db.get(ModelProvider, UUID(effective_pid)) if effective_pid else None
        if ep is not None and ep.status == "enabled" and (ep.params or {}).get("vision"):
            raise HTTPException(
                status.HTTP_428_PRECONDITION_REQUIRED,
                f"图片将上传至模型服务商「{ep.name}」处理，请确认内容不涉密后重试",
            )

    # 1) 落用户消息（content.attachments 供前端展示；引擎读 run.input 消费）
    msg = Message(
        conversation_id=conv.id,
        role="user",
        content={"text": body.text, "attachments": attachments},
    )
    db.add(msg)
    await db.flush()  # 拿 msg.id 供 FileRef 登记
    for att in attachments:
        await files_service.add_ref(
            db, UUID(att["file_id"]), "message", msg.id
        )
    # 首条消息自动命名：仍是默认标题时用消息首行生成（手动重命名过的不覆盖）
    if not conv.message_count and conv.title == "新会话":
        first_line = next((ln.strip() for ln in body.text.splitlines() if ln.strip()), "")
        if first_line:
            conv.title = first_line[:30] + ("…" if len(first_line) > 30 else "")
    conv.message_count = (conv.message_count or 0) + 1
    conv.last_message_at = datetime.now(UTC)

    # 2) 创建 run：预算快照创建时固化（数据库设计 §2.3）；附件随 input 快照
    run = Run(
        conversation_id=conv.id,
        agent_id=conv.agent_id,
        trigger="manual",
        input={
            "text": body.text,
            "model_provider_id": model_override,
            "attachment_ids": [att["file_id"] for att in attachments],
            # 涉密确认留痕（审计）：带图消息外发前已经用户确认
            "attachment_upload_confirmed": body.confirm_upload,
        },
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
