"""conversations 共享服务：Agent 解析、附件校验、message + run 创建。

抽出来的理由（M7a）：`POST /tasks`（新开主任务）与 `POST /conversations/{id}/messages`
（会话内继续）必须产生**完全一致**的 message + run + inbox 投递。任何分叉都会让引擎侧
出现「这条消息走 A 路径、那条走 B 路径」的特例分支，是后续 bug 的温床。
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import literal_column, select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agents.models import Agent
from app.modules.conversations.models import Conversation, Message
from app.modules.files import service as files_service
from app.modules.files.models import File
from app.modules.models_module.models import ModelProvider
from app.modules.runs.models import Run
from app.modules.tasks import service as tasks_service
from app.modules.tasks.models import Task


async def resolve_agent(db: AsyncSession, agent_id: UUID | None) -> Agent:
    """显式 agent_id 优先，否则取默认 Agent（is_default + enabled）。"""
    if agent_id:
        agent = await db.get(Agent, agent_id)
        if agent is None or agent.status != "enabled":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent 不存在或已停用")
        return agent
    agent = await db.scalar(
        select(Agent).where(Agent.is_default.is_(True), Agent.status == "enabled")
    )
    if agent is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "无可用 Agent，请先创建")
    return agent


async def resolve_model_override(db: AsyncSession, model_provider_id: UUID | None) -> str | None:
    """对话内临时换模型：校验可用性，随 run.input 快照固化（数据库设计 §2.3）。"""
    if model_provider_id is None:
        return None
    provider = await db.get(ModelProvider, model_provider_id)
    if provider is None or provider.status != "enabled" or provider.kind != "llm":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型不存在或不可用")
    return str(provider.id)


async def collect_attachments(
    db: AsyncSession, attachment_ids: list[UUID]
) -> tuple[list[dict[str, Any]], bool]:
    """批量校验附件并返回 (快照列表, 是否含图片)。"""
    if not attachment_ids:
        return [], False
    stmt = select(File).where(File.id.in_(attachment_ids))
    found = {f.id: f for f in (await db.scalars(stmt)).all()}
    missing = [str(i) for i in attachment_ids if i not in found]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"附件不存在：{', '.join(missing)}")
    attachments: list[dict[str, Any]] = [
        {"file_id": str(f.id), "filename": f.filename, "mime": f.mime, "size": f.size}
        for f in (found[i] for i in attachment_ids)
    ]
    has_image = any((a["mime"] or "").startswith("image/") for a in attachments)
    return attachments, has_image


async def assert_image_upload_confirmed(
    db: AsyncSession,
    *,
    has_image: bool,
    confirm_upload: bool,
    agent: Agent,
    model_override: str | None,
) -> None:
    """图片外发涉密确认门（ADR：带图且生效模型支持视觉 → 图片将上传至服务商）。

    未确认时 428，前端弹窗（含服务商名）确认后携 confirm_upload=true 重发。
    文档附件走本地 docling 容器提取，不出本机，无需确认。
    """
    if not has_image or confirm_upload:
        return
    effective_pid = model_override or str(agent.model_provider_id or "")
    ep = await db.get(ModelProvider, UUID(effective_pid)) if effective_pid else None
    if ep is not None and ep.status == "enabled" and (ep.params or {}).get("vision"):
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            f"图片将上传至模型服务商「{ep.name}」处理，请确认内容不涉密后重试",
        )


# 幂等键上限（P1-9）：与前端 UUID 长度不冲突，留足自造键空间
CLIENT_MESSAGE_ID_MAX = 64

# 必须与唯一索引 uq_runs_client_message_id 的表达式逐字一致，且走**字面量**：
# 键名一旦变成绑定参数（`input ->> $1`），PG 就认不出这是索引表达式，查重会退化成
# 全表扫描——索引只负责兜竞态，日常判重得吃到索引。
CLIENT_MESSAGE_ID_EXPR = "input ->> 'client_message_id'"


def normalize_client_message_id(raw: str | None) -> str | None:
    """空白视为"未提供"，避免前端传空串被当成一个真实键。"""
    key = (raw or "").strip()
    return key[:CLIENT_MESSAGE_ID_MAX] if key else None


async def find_run_by_client_message_id(
    db: AsyncSession, client_message_id: str, conversation_id: UUID | None = None
) -> Run | None:
    """按幂等键找既有 run（P1-9）。

    给了 conversation_id 就限定在会话内（与唯一索引同口径）；不给则全局找——
    `POST /tasks` 建会话之前就要判重，此时还没有会话 id。取最早一条：并发插入
    时后插入者被唯一索引挡下，先插入者才是"首次提交"。
    """
    stmt = select(Run).where(literal_column(CLIENT_MESSAGE_ID_EXPR) == client_message_id)
    if conversation_id is not None:
        stmt = stmt.where(Run.conversation_id == conversation_id)
    stmt = stmt.order_by(Run.created_at.asc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def create_user_run(
    db: AsyncSession,
    conv: Conversation,
    agent: Agent,
    *,
    text: str,
    attachments: list[dict[str, Any]],
    model_override: str | None = None,
    confirm_upload: bool = False,
    task: Task | None = None,
    client_message_id: str | None = None,
    provided_inputs: dict[str, Any] | None = None,
) -> Run:
    """落用户消息 + 创建 run + 投 inbox 事件（**不 commit**，由调用方事务收口）。

    task 非空时把 task_id / worker_name 快照进 run.input：引擎据此把 run 绑定到
    主任务（P1 的 engine-task-binding），后续 context_assembly 才能注入任务卡。

    client_message_id 非空时启用 run 级幂等（P1-9）：同会话同键的重复提交**不落
    消息、不建 run**，直接返回首次那个 run——消息、计数、inbox 事件一并不重复。
    """
    key = normalize_client_message_id(client_message_id)
    if key is not None:
        existing = await find_run_by_client_message_id(db, key, conv.id)
        if existing is not None:
            return existing

    msg = Message(
        conversation_id=conv.id,
        role="user",
        content={"text": text, "attachments": attachments},
    )
    db.add(msg)
    await db.flush()  # 拿 msg.id 供 FileRef 登记
    for att in attachments:
        await files_service.add_ref(db, UUID(att["file_id"]), "message", msg.id)

    # 首条消息自动命名：仍是默认标题时用消息首行生成（手动重命名过的不覆盖）
    if not conv.message_count and conv.title == "新会话":
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if first_line:
            conv.title = first_line[:30] + ("…" if len(first_line) > 30 else "")
            if task is not None:
                # 任务卡/看板标题跟随首条消息（设计上会话与主任务实例 1:1）
                await tasks_service.sync_task_title(db, conv.id, conv.title)
    conv.message_count = (conv.message_count or 0) + 1
    conv.last_message_at = datetime.now(UTC)

    # 任务实例首次收到消息 → 计时开始（重启已完成的旧任务时复位为 active）
    if task is not None and task.started_at is None:
        task.started_at = datetime.now(UTC)
        if task.status == "done":
            task.status = "active"
            task.finished_at = None

    run_input: dict = {
        "text": text,
        "model_provider_id": model_override,
        "attachment_ids": [att["file_id"] for att in attachments],
        # 涉密确认留痕（审计）：带图消息外发前已经用户确认
        "attachment_upload_confirmed": confirm_upload,
    }
    if key is not None:
        # 键进 run.input 快照：唯一索引建在这个表达式上，重放/排障也能直接看到
        run_input["client_message_id"] = key

    if provided_inputs:
        # 输入契约取值快照（P1-4）：引擎预检据此判定必需输入是否齐备；
        # 快照进 run.input 也让"这一轮到底给了什么"可追溯、可重放
        run_input["inputs"] = provided_inputs

    if task is not None:
        run_input["task_id"] = str(task.id)
        run_input["worker_name"] = task.worker_name
        run_input["worker_version"] = task.worker_version

    # 预算快照创建时固化（数据库设计 §2.3）；附件随 input 快照
    run = Run(
        conversation_id=conv.id,
        agent_id=conv.agent_id,
        trigger="manual",
        input=run_input,
        budget={
            "tool_budget": agent.tool_budget,
            "max_iterations": agent.max_iterations,
            "max_tokens_per_run": agent.max_tokens_per_run,
            "timeout_seconds": agent.timeout_seconds,
        },
    )
    db.add(run)
    try:
        await db.flush()  # 拿 run.id
    except IntegrityError:
        # 竞态兜底：同键两次提交并发穿过上面的查重，唯一索引只放行一个。
        # 整单回滚（连本请求的 message 一起丢弃），改用胜出者的 run——重复请求
        # 不留半截数据；此处返回的对象必须是重新查到的那一个（回滚后原实例已失效）。
        if key is None:
            raise
        await db.rollback()
        winner = await find_run_by_client_message_id(db, key, conv.id)
        if winner is None:
            raise
        return winner

    # 投 inbox + NOTIFY（唤醒引擎 worker）
    await db.execute(
        sa_text(
            "INSERT INTO inbox_events (event_type, target_run_id, payload, status) "
            "VALUES ('user_input', :rid, CAST(:p AS jsonb), 'new')"
        ),
        {"rid": run.id, "p": json.dumps({"run_id": str(run.id)}, ensure_ascii=False)},
    )
    await db.execute(sa_text("SELECT pg_notify('inbox_events', :rid)"), {"rid": str(run.id)})
    return run
