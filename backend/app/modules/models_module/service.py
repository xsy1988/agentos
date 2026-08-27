"""model_providers 管理面：CRUD + 连通性测试 + LLM_Gateway 同步。"""

from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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


async def sync_from_gateway(db: AsyncSession) -> dict:
    """从 LLM_Gateway 拉取模型清单并同步到 model_providers。

    同步策略（幂等，按 model_name 匹配）：
    - 既有记录：base_url / api_key 迁移为网关地址与网关 key（保留 name、params、
      Agent 绑定、run 快照、用量记账连续性）；记录上标记 gateway 管理来源。
    - 网关新模型：按 model_name 推断 kind（embedding 类后缀 → embedding）创建。
    - 平台有但网关没有的记录：不动（本地 vLLM 直连等历史接入仍可用）。
    """
    from app.modules.models_module.provider import encrypt_secret

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{settings.llm_gateway_base_url}/models",
                headers={"Authorization": f"Bearer {settings.llm_gateway_api_key}"},
            )
            resp.raise_for_status()
            models = resp.json().get("data", [])
    except httpx.HTTPError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"LLM_Gateway 不可达（{settings.llm_gateway_base_url}）: {e}",
        ) from e

    gateway_names = {m["id"] for m in models if m.get("id")}
    existing = {
        p.model_name: p for p in (await db.scalars(select(ModelProvider))).all()
    }

    migrated, created = 0, 0
    for name in gateway_names:
        prov = existing.get(name)
        if prov is not None:
            # 既有记录迁移：指向网关（幂等，重复同步无害）
            if (
                prov.base_url != settings.llm_gateway_base_url
                or prov.params.get("via_gateway") is not True
            ):
                prov.base_url = settings.llm_gateway_base_url
                prov.api_key_encrypted = encrypt_secret(settings.llm_gateway_api_key)
                params = dict(prov.params or {})
                params["via_gateway"] = True
                prov.params = params
                migrated += 1
            continue
        # 网关新模型：按命名推断 kind 后创建
        kind = "embedding" if "embed" in name.lower() else "llm"
        db.add(
            ModelProvider(
                kind=kind,
                name=name,
                impl="openai_compatible",
                base_url=settings.llm_gateway_base_url,
                api_key_encrypted=encrypt_secret(settings.llm_gateway_api_key),
                model_name=name,
                params={"via_gateway": True},
            )
        )
        created += 1

    await db.commit()
    return {
        "gateway": settings.llm_gateway_base_url,
        "total": len(gateway_names),
        "migrated": migrated,
        "created": created,
    }
