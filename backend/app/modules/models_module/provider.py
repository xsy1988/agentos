"""模型接入：Provider 工厂（模块详细设计 §2.5）。

LLMProvider 抽象落为 get_chat_model()：按 impl 构造 LangChain BaseChatModel，
engine 的 agent 节点直接 astream/bind_tools——LangGraph 生态原生流式与工具绑定。
密钥经 Fernet 加密落库，密钥由 settings.secret_key 派生（个人平台单密钥派生足够，
换密钥 = 换 SECRET_KEY 并重录 api_key，注释即文档）。
"""

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

from app.core.config import settings
from app.modules.models_module.models import ModelProvider


def _fernet() -> Fernet:
    """从 secret_key 派生 Fernet 密钥（sha256 → urlsafe base64，32 字节）。"""
    derived = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(plain: str) -> bytes:
    return _fernet().encrypt(plain.encode())


def decrypt_secret(token: bytes) -> str | None:
    try:
        return _fernet().decrypt(bytes(token)).decode()
    except (InvalidToken, ValueError):
        return None


def get_chat_model(
    provider: "ModelProvider | dict[str, Any]", api_key: str | None = None
) -> BaseChatModel:
    """按 impl 构造 chat 模型。api_key 显式传入（调用方先解密）。

    provider 接受 ORM 对象或 dict：engine 侧的 EngineBackend 协议返回 dict
    （二期 RPC 后仍是 dict），本模块内 service 用 ORM 对象。
    """
    if not isinstance(provider, dict):
        provider = {
            "impl": provider.impl,
            "base_url": provider.base_url,
            "model_name": provider.model_name,
            "params": provider.params,
        }
    params: dict = provider.get("params") or {}
    match provider["impl"]:
        case "openai_compatible":
            return ChatOpenAI(
                model=provider["model_name"],
                base_url=provider["base_url"],
                api_key=SecretStr(api_key or "not-set"),
                temperature=params.get("temperature", 0.3),
                max_tokens=params.get("max_tokens"),  # type: ignore[call-arg]
                timeout=params.get("timeout", 120),
                # 流式末帧携带 usage_metadata，供 budget_state 记账（不支持的端点会忽略）
                stream_usage=True,  # type: ignore[call-arg]
            )
        case _:
            raise ValueError(
                f"暂不支持的模型实现类型: {provider['impl']}（M2 仅 openai_compatible）"
            )


def get_embeddings(
    provider: "ModelProvider | dict[str, Any]", api_key: str | None = None
) -> Embeddings:
    """按 impl 构造 embedding 客户端（kind=embedding 的 provider）。

    与 get_chat_model 同构：接受 ORM 或 dict，密钥显式传入。
    Kimi 端点实测：POST /embeddings 自动路由 bge_m3_embed（1024 维）。
    """
    if not isinstance(provider, dict):
        provider = {
            "impl": provider.impl,
            "base_url": provider.base_url,
            "model_name": provider.model_name,
            "params": provider.params,
        }
    params: dict = provider.get("params") or {}
    match provider["impl"]:
        case "openai_compatible":
            return OpenAIEmbeddings(
                model=provider["model_name"],
                base_url=provider["base_url"],
                api_key=SecretStr(api_key or "not-set"),
                timeout=params.get("timeout", 30),
                # 端点不支持 tiktoken 分词计数，统一按条目切批（防超长请求报错）
                check_embedding_ctx_length=False,  # type: ignore[call-arg]
            )
        case _:
            raise ValueError(
                f"暂不支持的 embedding 实现类型: {provider['impl']}（M3 仅 openai_compatible）"
            )
