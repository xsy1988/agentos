"""workers 请求/响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------- Worker 级 ----------


class SubWorkerOut(BaseModel):
    """子任务（sub_workers/<ref>/WORKER.md 的摘要）。"""

    ref: str
    name: str
    seq: int
    kind: Literal["main", "branch"]
    optional: bool
    description: str
    capability_hint: list[str] = Field(default_factory=list)


class WorkerVersionOut(BaseModel):
    version: str
    active: bool
    latest: bool
    created_at: datetime | None = None


class WorkerOut(BaseModel):
    """Worker 概览（列表页 + 详情页头部）。"""

    name: str
    description: str = ""
    icon: str | None = None
    color: str | None = None
    enabled: bool = True
    active_version: str | None = None  # 实际生效版本（effective）
    pinned_version: str | None = None  # manifest 里显式指定的版本（None=跟随最新）
    latest_version: str | None = None
    versions: list[WorkerVersionOut] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    references: list[dict] | None = None
    playbook: str = ""
    sub_workers: list[SubWorkerOut] = Field(default_factory=list)
    has_files: bool = True  # 生效版本目录是否存在

    @field_validator("icon", mode="before")
    @classmethod
    def _icon(cls, v: object) -> object:
        return v or None


class WorkerCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    icon: str | None = Field(default=None, max_length=32)
    color: str | None = Field(default=None, max_length=16)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Worker 名称不能为空")
        return v


class WorkerPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    active_version: str | None = Field(default=None, min_length=2, max_length=8)


class SubWorkerCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: Literal["main", "branch"] = "main"
    description: str = Field(default="", max_length=4000)


# ---------- 版本内文件 ----------


class FileNodeOut(BaseModel):
    name: str
    type: Literal["dir", "file"]
    children: list["FileNodeOut"] = Field(default_factory=list)


class WorkerFileOut(BaseModel):
    path: str
    version: str
    writable: bool
    content: str


class WorkerFileIn(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)


class WorkerFileCreateIn(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(default="", max_length=200_000)


class VersionBuildOut(BaseModel):
    version: str
    copied_from: str


# ---------- 工具引用清单校验 ----------


class CapabilityRefOut(BaseModel):
    name: str
    found: bool
    capability_id: str | None = None
    type: str | None = None
    risk_level: str | None = None
    enabled: bool | None = None


class CapabilityRefsOut(BaseModel):
    version: str
    references: list[CapabilityRefOut]
    missing: list[str]
