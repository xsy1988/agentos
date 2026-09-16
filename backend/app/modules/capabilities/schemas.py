"""capabilities 请求/响应模型（模块详细设计 §1.3）。

统一 payload 结构按 type 解释：
- tool：payload.builtin（内置占位）或 payload.schema（OpenAI 函数签名）
- skill：payload.skill_md（SKILL.md 全文，yaml 头 name/description/version）
- mcp：payload.transport(stdio/http) + launch/url + env + secret_env
- plugin：可纯前端（展示类），payload.frontend 声明前端清单（§3.5）——
  mode=iframe{url,sandbox,allowlist_origin} 或 server_driven{schema}；
  也可携带后端 transport（同 mcp）。frontend 与 transport 至少其一。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

VALID_TYPES = ("tool", "skill", "mcp", "plugin")
VALID_CATEGORIES = ("external", "internal", "builtin")
VALID_RISK_LEVELS = ("read", "write", "dangerous")
VALID_MODES = ("pinned", "semantic")


class CapabilityCreateIn(BaseModel):
    type: str = Field(pattern=r"^(tool|skill|mcp|plugin)$")
    category: str = Field(default="external", pattern=r"^(external|internal|builtin)$")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    description: str = Field(min_length=1, max_length=2000)
    version: str = Field(default="0.1.0", max_length=32)
    risk_level: str = Field(default="read", pattern=r"^(read|write|dangerous)$")
    payload: dict[str, Any] = Field(default_factory=dict)
    # 注册冒烟用例：[{input: {...}, expected: "..."}]，空则只做连通性检查
    test_info: list[dict[str, Any]] | None = None
    # mcp/plugin 的敏感环境变量（Fernet 加密落库，只进不出）
    secret_env: dict[str, str] | None = Field(default=None, max_length=20)


class CapabilityUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, min_length=1, max_length=2000)
    version: str | None = Field(default=None, max_length=32)
    risk_level: str | None = Field(default=None, pattern=r"^(read|write|dangerous)$")
    payload: dict[str, Any] | None = None
    test_info: list[dict[str, Any]] | None = None
    secret_env: dict[str, str] | None = Field(default=None, max_length=20)
    # 能力级开关（M3 冒烟通过的才能置 true）
    enabled: bool | None = None


class CapabilityToolOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tool_name: str
    description: str
    enabled: bool


class CapabilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    category: str
    name: str
    description: str
    version: str
    risk_level: str
    payload: dict[str, Any]  # secret_env 已剥离
    has_secret_env: bool
    test_info: list[dict[str, Any]] | None
    enabled: bool
    health_status: str
    last_health_check_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CapabilitySmokeReport(BaseModel):
    """注册冒烟报告：逐条用例结果 + 汇总。"""

    passed: bool
    checks: list[dict[str, Any]]
    summary: str


class BindingIn(BaseModel):
    capability_id: UUID
    mode: str = Field(default="semantic", pattern=r"^(pinned|semantic)$")


class BindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    capability_id: UUID
    mode: str
    created_at: datetime
