"""P0-4 等待服务：登记 / 出网标记 / 回调落定 / 超时 / 唤醒投递。

纪律：
- 所有函数不收尾提交（`expire_due`/`resolve` 由调用方与 inbox 投递同事务提交），
  避免"状态已翻但 run 没被唤醒"的缝隙；`cancel_run_awaits` 例外（终态收尾，独立提交）。
- 状态迁移一律 CAS（`WHERE status='waiting'`），`False`/空返回 = 竞争落败，
  调用方不得重复投递唤醒事件。
"""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Integer, cast, func, literal, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.awaits.models import AWAIT_TERMINAL_STATUSES, AwaitBroker

# 回调凭据的域分隔前缀：同一 secret_key 派生多种凭据时互不通用
_CALLBACK_PURPOSE = "await-callback"


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def make_idempotency_key(args: dict[str, Any]) -> str:
    """参数指纹：同 run + 同工具 + 同参数 = 同一笔外部请求（防重复派发/重复下单）。"""
    payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def make_callback_token(await_id: str, *, secret: str | None = None) -> str:
    """回调专用凭据（与 X-API-Key 双重鉴权）：泄露一次不牵连其它等待。"""
    key = (secret if secret is not None else settings.secret_key).encode("utf-8")
    msg = f"{_CALLBACK_PURPOSE}|{await_id}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def verify_callback_token(await_id: str, token: str | None, *, secret: str | None = None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(make_callback_token(await_id, secret=secret), token)


def callback_url(await_id: str) -> str:
    """外部服务回传结果的地址（平台侧公网可达地址，见 settings.platform_base_url）。"""
    base = settings.platform_base_url.rstrip("/")
    return f"{base}/api/v1/open/awaits/{await_id}/resolve"


def pickup_url_template(await_id: str) -> str:
    """取件地址模板（P2-1）：`{file_id}` 由外部服务用实际文件 id 替换。

    取件范围由 `await_id` 锁定为**本次等待所属 run**（平台侧反查归属，无列举入口），
    凭据是同一个 `callback_token`（走 `X-Callback-Token` 头，不进 URL 查询串——
    避免 token 落进访问日志）。
    """
    base = settings.platform_base_url.rstrip("/")
    return f"{base}/api/v1/open/files/{{file_id}}/content?await_id={await_id}"


def _waited_ms(row: AwaitBroker) -> int:
    if row.resolved_at is None:
        return int(row.waited_ms or 0)
    base = row.created_at or row.resolved_at
    return max(0, int((row.resolved_at - base).total_seconds() * 1000))


def brief(row: AwaitBroker) -> dict[str, Any]:
    """API 视图：不含 callback_token（凭据只在登记响应里下发一次）。"""
    return {
        "await_id": str(row.id),
        "run_id": str(row.run_id),
        "tool": row.tool_name,
        "status": row.status,
        "deadline_at": row.deadline_at,
        "waited_ms": int(row.waited_ms or 0),
        "attempts": int(row.attempts or 0),
        "notified_at": row.notified_at,
        "resolved_at": row.resolved_at,
        "error": row.error,
    }


def resume_payload(
    row: AwaitBroker,
    *,
    status: str,
    payload: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """投给引擎的扁平恢复载荷（中断恢复值形态，与 confirmation 同理）。

    带 `kind="await"` 便于 runtime 区分"等外部"与"等用户确认"两类恢复。
    """
    return {
        "kind": "await",
        "await_id": str(row.id),
        "tool": row.tool_name,
        "status": status,
        "payload": payload,
        "error": error,
        "waited_ms": _waited_ms(row),
        "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
    }


async def ensure_await(
    db: AsyncSession,
    *,
    run_id: uuid.UUID | str,
    tool_name: str,
    idempotency_key: str,
    timeout_seconds: int | None = None,
    conversation_id: uuid.UUID | str | None = None,
    task_id: uuid.UUID | str | None = None,
    step_id: uuid.UUID | str | None = None,
    capability_id: str | None = None,
    payload_in: dict[str, Any] | None = None,
) -> tuple[AwaitBroker, bool]:
    """幂等登记等待行：命中既有行直接复用（防同参重放导致重复派发）。

    返回 `(row, created)`；`created=True` 表示本次首次登记（调用方负责出网派发）。
    """
    run_uuid = _as_uuid(run_id)
    assert run_uuid is not None
    existing = await db.scalar(
        select(AwaitBroker).where(
            AwaitBroker.run_id == run_uuid,
            AwaitBroker.tool_name == tool_name,
            AwaitBroker.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, False

    timeout = (
        settings.await_default_timeout_seconds
        if timeout_seconds is None
        else max(1, int(timeout_seconds))
    )
    now = datetime.now(UTC)
    await_id = uuid.uuid4()
    row = AwaitBroker(
        id=await_id,
        run_id=run_uuid,
        conversation_id=_as_uuid(conversation_id),
        task_id=_as_uuid(task_id),
        step_id=_as_uuid(step_id),
        capability_id=capability_id,
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        callback_token=make_callback_token(str(await_id)),
        status="waiting",
        deadline_at=now + timedelta(seconds=timeout),
        payload_in=payload_in,
        attempts=0,
        waited_ms=0,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # 并发竞态：另一路已登记同一笔（复合唯一约束兜底）
        await db.rollback()
        again = await db.scalar(
            select(AwaitBroker).where(
                AwaitBroker.run_id == run_uuid,
                AwaitBroker.tool_name == tool_name,
                AwaitBroker.idempotency_key == idempotency_key,
            )
        )
        if again is None:
            raise
        return again, False
    await db.refresh(row)
    return row, True


async def mark_notified(
    db: AsyncSession,
    row: AwaitBroker,
    *,
    at: datetime | None = None,
    response: dict[str, Any] | None = None,
) -> None:
    """标记「已出网派发」：重放/重启时据此跳过重复出网。

    `response` 并存进 `payload_in`，重放时可直接复述首次派发结果。
    """
    row.notified_at = row.notified_at or at or datetime.now(UTC)
    if response is not None:
        row.payload_in = {**(row.payload_in or {}), "dispatch_response": response}
    await db.commit()


async def resolve(
    db: AsyncSession,
    await_id: uuid.UUID | str,
    *,
    status: str = "granted",
    payload: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> tuple[AwaitBroker | None, bool]:
    """回调落定：CAS `waiting → status`。

    返回 `(row, won)`；`won=False` 表示已被别的路径落定（幂等重放/超时先到），
    调用方只回既有状态、不重复唤醒 run。不提交事务（与 inbox 投递同事务）。
    """
    if status not in AWAIT_TERMINAL_STATUSES:
        raise ValueError(f"非法落定状态：{status}")
    row = await db.get(AwaitBroker, _as_uuid(await_id))
    if row is None:
        return None, False
    now = datetime.now(UTC)
    waited = max(0, int((now - (row.created_at or now)).total_seconds() * 1000))
    flipped = await db.scalar(
        update(AwaitBroker)
        .where(AwaitBroker.id == row.id, AwaitBroker.status == "waiting")
        .values(
            status=status,
            payload_out=payload,
            error=error,
            resolved_at=now,
            waited_ms=waited,
            attempts=AwaitBroker.attempts + 1,
            updated_at=now,
        )
        .returning(AwaitBroker.id)
        .execution_options(synchronize_session=False)
    )
    await db.refresh(row)
    return row, flipped is not None


def _expired_row_values(now: datetime) -> dict[str, Any]:
    waited = func.greatest(
        0,
        cast(
            func.extract(
                "epoch",
                literal(now, DateTime(timezone=True)) - AwaitBroker.created_at,
            )
            * 1000,
            Integer,
        ),
    )
    return {
        "status": "expired",
        "resolved_at": now,
        "waited_ms": waited,
        "error": {"code": "await_expired", "message": "外部流程超时未回传结果"},
        "updated_at": now,
    }


async def expire_due(db: AsyncSession, *, now: datetime | None = None) -> list[AwaitBroker]:
    """超时巡检：把已过 deadline 的 waiting 行 CAS 成 expired，返回被本次翻掉的行。

    返回空 = 无到期行（或被并发路径抢先翻掉）。不提交事务。
    """
    moment = now or datetime.now(UTC)
    ids = list(
        (
            await db.scalars(
                update(AwaitBroker)
                .where(
                    AwaitBroker.status == "waiting",
                    AwaitBroker.deadline_at <= moment,
                )
                .values(**_expired_row_values(moment))
                .returning(AwaitBroker.id)
                .execution_options(synchronize_session=False)
            )
        ).all()
    )
    if not ids:
        return []
    rows = await db.scalars(select(AwaitBroker).where(AwaitBroker.id.in_(ids)))
    return list(rows.all())


async def cancel(db: AsyncSession, row: AwaitBroker, *, reason: str | None = None) -> bool:
    """人工/终态撤销：CAS `waiting → cancelled`。不提交事务。"""
    now = datetime.now(UTC)
    waited = max(0, int((now - (row.created_at or now)).total_seconds() * 1000))
    flipped = await db.scalar(
        update(AwaitBroker)
        .where(AwaitBroker.id == row.id, AwaitBroker.status == "waiting")
        .values(
            status="cancelled",
            resolved_at=now,
            waited_ms=waited,
            error={"code": "await_cancelled", "message": reason or "等待被撤销"},
            attempts=AwaitBroker.attempts + 1,
            updated_at=now,
        )
        .returning(AwaitBroker.id)
        .execution_options(synchronize_session=False)
    )
    await db.refresh(row)
    return flipped is not None


async def cancel_run_awaits(
    db: AsyncSession, run_id: uuid.UUID | str, *, reason: str | None = None
) -> int:
    """run 收尾：把仍挂着的等待行关掉（防僵尸行）。独立提交。"""
    now = datetime.now(UTC)
    result = await db.execute(
        update(AwaitBroker)
        .where(AwaitBroker.run_id == _as_uuid(run_id), AwaitBroker.status == "waiting")
        .values(
            status="cancelled",
            resolved_at=now,
            error={"code": "await_cancelled", "message": reason or "run 已结束"},
            updated_at=now,
        )
        .returning(AwaitBroker.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return len(result.all())


async def list_awaits(
    db: AsyncSession,
    *,
    run_id: uuid.UUID | str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[AwaitBroker]:
    stmt = select(AwaitBroker).order_by(AwaitBroker.created_at.desc()).limit(limit)
    if run_id is not None:
        stmt = stmt.where(AwaitBroker.run_id == _as_uuid(run_id))
    if status:
        stmt = stmt.where(AwaitBroker.status == status)
    return list((await db.scalars(stmt)).all())


def _inbox_payload(row: AwaitBroker, *, status: str) -> dict[str, Any]:
    return resume_payload(
        row,
        status=status,
        payload=row.payload_out,
        error=row.error,
    )


async def enqueue_resume(db: AsyncSession, row: AwaitBroker, *, status: str) -> None:
    """投递唤醒事件（`resume`）：引擎 worker 拉到后从 interrupt 检查点恢复。

    与状态 CAS 同事务提交（调用方 commit），保证"翻状态必唤醒、不唤醒不翻状态"。
    """
    payload = json.dumps(_inbox_payload(row, status=status), ensure_ascii=False)
    await db.execute(
        text(
            "INSERT INTO inbox_events (event_type, target_run_id, payload, status) "
            "VALUES ('resume', :rid, CAST(:p AS jsonb), 'new')"
        ),
        {"rid": row.run_id, "p": payload},
    )
    await db.execute(text("SELECT pg_notify('inbox_events', :rid)"), {"rid": str(row.run_id)})
