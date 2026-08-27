"""模型接入路由：CRUD + 连通性测试。"""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.models_module import service
from app.modules.models_module.models import ModelProvider
from app.modules.models_module.schemas import (
    ModelProviderCreateIn,
    ModelProviderOut,
    ModelProviderUpdateIn,
)

router = APIRouter(
    prefix="/models",
    tags=["models"],
    dependencies=[Depends(get_current_user)],
)


def _to_out(p: ModelProvider) -> ModelProviderOut:
    data = {
        "id": p.id,
        "kind": p.kind,
        "name": p.name,
        "impl": p.impl,
        "base_url": p.base_url,
        "model_name": p.model_name,
        "params": p.params,
        "limits": p.limits,
        "status": p.status,
        "has_api_key": p.api_key_encrypted is not None,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }
    return ModelProviderOut.model_validate(data)


@router.get("", response_model=list[ModelProviderOut])
async def list_models(
    kind: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[ModelProviderOut]:
    return [_to_out(p) for p in await service.list_providers(db, kind=kind)]


@router.post("/sync-gateway")
async def sync_gateway(db: AsyncSession = Depends(get_db)) -> dict:
    """从 LLM_Gateway 同步模型清单（幂等：既有迁移 + 新模型创建）。"""
    return await service.sync_from_gateway(db)


@router.post("", response_model=ModelProviderOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelProviderCreateIn, db: AsyncSession = Depends(get_db)
) -> ModelProviderOut:
    return _to_out(await service.create_provider(db, body))


@router.patch("/{model_id}", response_model=ModelProviderOut)
async def update_model(
    model_id: UUID, body: ModelProviderUpdateIn, db: AsyncSession = Depends(get_db)
) -> ModelProviderOut:
    provider = await service.get_provider_or_404(db, model_id)
    return _to_out(await service.update_provider(db, provider, body))


@router.post("/{model_id}/test")
async def test_model(model_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    provider = await service.get_provider_or_404(db, model_id)
    return await service.test_provider(db, provider)
