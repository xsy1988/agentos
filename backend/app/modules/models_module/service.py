"""model_providers 管理面：CRUD + 连通性测试。"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.models_module.models import ModelProvider
from app.modules.models_module.schemas import ModelProviderCreateIn, ModelProviderUpdateIn


async def list_providers(db: AsyncSession, kind: str | None = None) -> list[ModelProvider]:
    stmt = select(ModelProvider).order_by(ModelProvider.created_at)
    if kind:
        stmt = stmt.where(ModelProvider.kind == kind)
    return list((await db.scalars(stmt)).all())


async def get_provider_or_404(db: AsyncSession, provider_id: UUID) -> ModelProvider:
    provider = await db.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型接入不存在")
    return provider


async def create_provider(db: AsyncSession, body: ModelProviderCreateIn) -> ModelProvider:
    from app.modules.models_module.provider import encrypt_secret

    provider = ModelProvider(
        kind=body.kind,
        name=body.name,
        impl=body.impl,
        base_url=body.base_url,
        api_key_encrypted=encrypt_secret(body.api_key) if body.api_key else None,
        model_name=body.model_name,
        params=body.params,
        limits=body.limits,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return provider


async def update_provider(
    db: AsyncSession, provider: ModelProvider, body: ModelProviderUpdateIn
) -> ModelProvider:
    from app.modules.models_module.provider import encrypt_secret

    data = body.model_dump(exclude_unset=True)
    if "api_key" in data:
        api_key = data.pop("api_key")
        provider.api_key_encrypted = encrypt_secret(api_key) if api_key else None
    for field, value in data.items():
        setattr(provider, field, value)
    await db.commit()
    await db.refresh(provider)
    return provider


async def test_provider(db: AsyncSession, provider: ModelProvider) -> dict:
    """连通性测试：llm 真实调一次 1-token 补全；embedding 真实向量化一条查询。"""
    from app.modules.models_module.provider import (
        decrypt_secret,
        get_chat_model,
        get_embeddings,
    )

    if provider.impl != "openai_compatible":
        return {"ok": False, "detail": f"impl={provider.impl} 暂不支持测试"}
    api_key = decrypt_secret(provider.api_key_encrypted or b"")
    try:
        if provider.kind == "embedding":
            # embedding 模型没有 chat 端点，走真实向量化并回报维度
            embeddings = get_embeddings(provider, api_key)
            vector = await embeddings.aembed_query("连通性测试")
            return {"ok": True, "reply_preview": f"向量化成功，维度 {len(vector)}"}
        llm = get_chat_model(provider, api_key)
        resp = await llm.ainvoke("hi")
        return {"ok": True, "reply_preview": str(resp.content)[:80]}
    except Exception as e:  # noqa: BLE001 —— 测试端点要如实回报任意失败
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"[:200]}
