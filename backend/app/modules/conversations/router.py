"""conversations 路由：会话管理 + 发消息（异步投递，接口永远快）。

发消息不同步执行（模块详细设计 §2.2）：
落 message → 创建 run(pending) → 投 inbox_events + NOTIFY → 返回 run_id。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.agents.models import Agent
from app.modules.auth.deps import get_current_user
from app.modules.conversations import service as conv_service
from app.modules.conversations.models import Conversation, Message
from app.modules.conversations.schemas import (
    ConversationCreateIn,
    ConversationOut,
    ConversationUpdateIn,
    MessageIn,
    MessageOut,
    SendMessageOut,
    SendMessageRunCreated,
    SendMessageTaskSwitch,
    TaskSwitchSuggestion,
)
from app.modules.runs.models import Run
from app.modules.tasks import service as tasks_service
from app.modules.tasks.service import COMMON_WORKER, worker_display
from app.modules.workers.inputs import InputContractError, sanitize_provided_inputs

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
    """新建会话（= 新的主任务实例，归属内建「通用任务」，除非走 POST /tasks）。

    会话与主任务实例 1:1（ADR-26）：这里同时建立任务实例，让任务看板不会漏掉
    任何会话。真正选择主任务由 `POST /tasks` 负责，它会改写 worker_name。
    """
    agent = await conv_service.resolve_agent(db, body.agent_id)
    conv = Conversation(agent_id=agent.id, title=body.title)
    db.add(conv)
    await db.flush()
    await tasks_service.ensure_task_for_conversation(db, conv)
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
async def delete_conversation(conversation_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
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

    try:
        provided_inputs = sanitize_provided_inputs(body.inputs)
    except InputContractError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    model_override = await conv_service.resolve_model_override(db, body.model_provider_id)
    attachments, has_image = await conv_service.collect_attachments(db, body.attachment_ids)
    await conv_service.assert_image_upload_confirmed(
        db,
        has_image=has_image,
        confirm_upload=body.confirm_upload,
        agent=agent,
        model_override=model_override,
    )

    # 会话↔主任务实例 1:1：老会话（迁移遗漏）在此惰性补建，引擎侧无需判空分叉
    task = await tasks_service.ensure_task_for_conversation(db, conv)

    # 新主任务检测（ADR-27，两条路径）：Worker 清单来自文件注册中心（workers.registry）
    # ① 零选择新建的会话归「通用任务」：首条消息高置信命中业务 Worker 时
    #   **自动改绑**（不弹软提示、不换会话），Agent 据此判定任务类型；
    # ② 已在某个业务 Worker 上的会话命中另一 Worker：保持软提示（用户抽板）。
    if not body.force_current_task and body.text.strip() and not body.attachment_ids:
        suggestion = await tasks_service.detect_task_switch(
            db, body.text, current_worker_name=task.worker_name
        )
        if suggestion is not None:
            if task.worker_name in ("", COMMON_WORKER):
                # 路径①：通用任务 → 高置信 Worker，直接改绑后照常建 run。
                # 本条消息就是判定依据，改绑后 run 的任务卡携带该 Worker 的 L1/L2。
                await tasks_service.rebind_worker(db, task, suggestion["worker_name"])
            else:
                # 路径②：业务 Worker 之间的切换交回用户抽板
                # 提交惰性补建的 task（若有），再返回软提示
                await db.commit()
                return SendMessageTaskSwitch(
                    conversation_id=conv.id,
                    suggested_worker=TaskSwitchSuggestion(**suggestion),
                    pending_text=body.text,
                    current_task_name=worker_display(task.worker_name),
                )

    if body.force_current_task and body.text.strip():
        # 用户选择「仍在本会话继续」：留痕，供任务卡提示模型聚焦当前主任务
        await tasks_service.append_out_of_scope(db, task, body.text)

    run = await conv_service.create_user_run(
        db,
        conv,
        agent,
        text=body.text,
        attachments=attachments,
        model_override=model_override,
        confirm_upload=body.confirm_upload,
        task=task,
        client_message_id=body.client_message_id,
        provided_inputs=provided_inputs,
    )
    await db.commit()
    return SendMessageRunCreated(conversation_id=conv.id, run_id=run.id)
