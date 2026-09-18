"""model_providers 请求/响应模型。密钥只进不出。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_limits(limits: dict) -> dict:
    """limits 取值校验（方案 §5 P1-2）。

    只校验已知键的类型与非负性，未知键原样保留（供应商特定扩展向前兼容）。
    校验放在 schema：错配的限流值比"没配"更危险（写错 0.0001 会把 run 拖死）。
    """
    for key in ("rate_limit_rps", "rate_limit_tpm"):
        value = limits.get(key)
        if value is None:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key} 必须是数字") from e
        if num < 0:
            raise ValueError(f"{key} 不能为负（0 = 不限）")
    return limits


class ModelProviderBase(BaseModel):
    kind: str = Field(pattern=r"^(llm|embedding)$")
    name: str = Field(min_length=1, max_length=128)
    impl: str = Field(pattern=r"^(openai_compatible|anthropic|ollama)$")
    base_url: str = Field(min_length=1, max_length=255)
    model_name: str = Field(min_length=1, max_length=128)
    params: dict = Field(default_factory=dict)
    # 限流与配额（P1-2 起有执行语义）：rate_limit_rps / rate_limit_tpm，
    # 0 或缺失 = 不限；缺省走 settings.model_default_rate_limit_*
    limits: dict = Field(default_factory=dict)

    @field_validator("limits")
    @classmethod
    def _check_limits(cls, value: dict) -> dict:
        return _validate_limits(value)


class ModelProviderCreateIn(ModelProviderBase):
    api_key: str | None = Field(default=None, max_length=512)  # 明文只在此出现一次


class ModelProviderUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=255)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)
    params: dict | None = None
    limits: dict | None = None

    @field_validator("limits")
    @classmethod
    def _check_limits(cls, value: dict | None) -> dict | None:
        return _validate_limits(value) if value is not None else None

    status: str | None = Field(default=None, pattern=r"^(enabled|disabled)$")


class ModelProviderOut(ModelProviderBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    has_api_key: bool  # 只报有无，不回内容
    created_at: datetime
    updated_at: datetime
