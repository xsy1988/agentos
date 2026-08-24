"""skills_forge 路由：草稿审核流（列表/diff/批准/驳回）+ 手动触发复盘。

批准即转 capabilities(type=skill) 并写语义向量 → 下次同类任务被语义发现命中。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.capabilities.models import Capability
from app.modules.skills_forge import service
from app.modules.skills_forge.models import SkillProposal

router = APIRouter(
    prefix="/skills",
    tags=["skills"],
    dependencies=[Depends(get_current_user)],
)


class ProposalOut(BaseModel):
    id: str
    run_id: str
    trigger: str
    draft_md: str
    similar_to_capability_id: str | None
    similar_to_name: str | None = None
    similar_to_current_md: str | None = None  # diff 用：已有技能当前全文
    status: str
    review_note: str | None
    reviewed_at: str | None
    created_at: str


class ReviewIn(BaseModel):
    review_note: str | None = None


async def _to_out(db: AsyncSession, p: SkillProposal) -> ProposalOut:
    similar_name: str | None = None
    current_md: str | None = None
    if p.similar_to_capability_id is not None:
        cap = await db.get(Capability, p.similar_to_capability_id)
        if cap is not None:
            similar_name = cap.name
            current_md = str((cap.payload or {}).get("skill_md") or "")
    return ProposalOut(
        id=str(p.id),
        run_id=str(p.run_id),
        trigger=p.trigger,
        draft_md=p.draft_md,
        similar_to_capability_id=(
            str(p.similar_to_capability_id) if p.similar_to_capability_id else None
        ),
        similar_to_name=similar_name,
        similar_to_current_md=current_md,
        status=p.status,
        review_note=p.review_note,
        reviewed_at=p.reviewed_at.isoformat() if p.reviewed_at else None,
        created_at=p.created_at.isoformat(),
    )


@router.get("/proposals", response_model=list[ProposalOut])
async def list_proposals(
    status: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[ProposalOut]:
    q = select(SkillProposal).order_by(SkillProposal.created_at.desc()).limit(100)
    if status:
        q = q.where(SkillProposal.status == status)
    rows = (await db.execute(q)).scalars().all()
    return [await _to_out(db, p) for p in rows]


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
async def get_proposal(
    proposal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ProposalOut:
    p = await db.get(SkillProposal, proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return await _to_out(db, p)


@router.post("/proposals/{proposal_id}/approve")
async def approve(
    proposal_id: uuid.UUID, body: ReviewIn | None = None, db: AsyncSession = Depends(get_db)
) -> dict:
    """批准：草稿转 capabilities(type=skill)，语义向量入检索池。"""
    note = body.review_note if body else None
    result = await service.approve_proposal(str(proposal_id), note)
    if "error" in result:
        raise HTTPException(status_code=409, detail=str(result["error"]))
    return result


@router.post("/proposals/{proposal_id}/reject")
async def reject(
    proposal_id: uuid.UUID, body: ReviewIn | None = None, db: AsyncSession = Depends(get_db)
) -> dict:
    note = body.review_note if body else None
    result = await service.reject_proposal(str(proposal_id), note)
    if "error" in result:
        raise HTTPException(status_code=409, detail=str(result["error"]))
    return result


@router.post("/review/{run_id}")
async def review(run_id: uuid.UUID, trigger: str = "success") -> dict:
    """手动触发复盘（正常由 hooks.on_run_end 自动发起；触发器测试/运营用）。"""
    if trigger not in ("success", "correction"):
        raise HTTPException(status_code=422, detail="trigger 只能是 success/correction")
    result = await service.review_run(str(run_id), trigger)
    return result or {"skipped": "run 不存在或无素材"}
