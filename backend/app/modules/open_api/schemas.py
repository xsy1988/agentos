"""open_api 请求/响应模型。

第三方提交一个「Worker + 能力清单」捆绑包：
- capabilities 逐项注册（结构同平台内 CapabilityCreateIn）；同名已存在则跳过（幂等）
- worker.capabilities 为能力名引用清单，必须全部命中（平台已有 ∪ 本次提交），否则 422
- Worker 已存在时按 if_exists 决策：fail(409) / skip / new_version（保留历史的版本演进）
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.capabilities.schemas import CapabilityCreateIn


class OpenInputSpec(BaseModel):
    """一条输入声明（P1-4）；未知字段直接 422（拼错的声明比缺声明更危险）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    type: Literal["text", "number", "date", "file", "url", "json"] = "text"
    required: bool = False
    description: str = Field(default="", max_length=500)
    example: str = Field(default="", max_length=200)

    def to_registry(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
            "example": self.example,
        }


class OpenSubWorkerIn(BaseModel):
    """子任务定义（落为 sub_workers/<名>/WORKER.md）。"""

    name: str = Field(min_length=1, max_length=255)
    seq: int = Field(default=0, ge=0, le=999)
    kind: Literal["main", "branch"] = "main"
    # 缺省按 kind 推断：branch → true，main → false
    optional: bool | None = None
    description: str = Field(default="", max_length=4000)
    playbook: str = Field(default="", max_length=200_000)
    capability_hint: list[str] = Field(default_factory=list, max_length=32)
    # 输入声明（P1-4）：子任务级只做解析/校验/展示，run 级门由主 Worker 声明
    inputs: list[OpenInputSpec] = Field(default_factory=list, max_length=16)


class OpenWorkerIn(BaseModel):
    """Worker 定义（落为 data/workers/<名>/vN/WORKER.md 文件包）。"""

    name: str = Field(min_length=1, max_length=128)
    # L1 元信息：常驻上下文，供看板展示与新主任务语义检测命中——必填且必须讲清场景
    description: str = Field(min_length=1, max_length=4000)
    icon: str | None = Field(default=None, max_length=32)
    color: str | None = Field(default=None, max_length=16)
    # 工具引用清单（能力名）：可指向本次提交的 capabilities 或平台已有能力
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    # L3 引用清单（可指向平台文档 anchor / plugin 能力名 / skill 能力名）
    references: list[dict[str, Any]] | None = None
    # L2 正文 playbook（五件事 + 第零步依赖预检）；空则落脚手架模板（注册后需补写）
    playbook: str = Field(default="", max_length=200_000)
    # 输入契约（P1-4）：缺任一必填项时平台在调用模型前拦截（不靠模型自觉）
    inputs: list[OpenInputSpec] = Field(default_factory=list, max_length=16)
    sub_workers: list[OpenSubWorkerIn] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Worker 名称不能为空")
        if "/" in v or "\\" in v or v.startswith(".") or v in (".", ".."):
            raise ValueError("Worker 名称不允许包含路径分隔符或以点开头")
        return v


class OpenWorkerRegisterIn(BaseModel):
    """POST /open/workers/register 请求体。"""

    worker: OpenWorkerIn
    capabilities: list[CapabilityCreateIn] = Field(default_factory=list, max_length=64)
    if_exists: Literal["fail", "skip", "new_version"] = "fail"
    # 可选：目标 Agent 名单。填了就比对这些 Agent 的 `tool_budget`，把"装不下"作为
    # warnings 回报（非阻断）；缺省 = 零影响，注册结果与从前逐字相同。
    target_agents: list[str] = Field(default_factory=list, max_length=32)


class OpenCapabilityResultOut(BaseModel):
    """单个能力的注册结果。"""

    name: str
    type: str
    # created=新建且冒烟通过；smoke_failed=新建但冒烟未过（落库 disabled，修复后重试）；
    # exists=同名已存在，本次跳过（幂等，不覆盖既有配置）
    status: Literal["created", "smoke_failed", "exists"]
    enabled: bool
    smoke_summary: str = ""


class OpenWorkerRegisterOut(BaseModel):
    """注册结果汇总。"""

    worker_name: str
    action: Literal["created", "skipped", "new_version"]
    version: str | None
    capability_results: list[OpenCapabilityResultOut] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OpenCapabilityBriefOut(BaseModel):
    """能力目录条目（供第三方查名冲突 / 引用平台已有能力）。"""

    name: str
    type: str
    category: str
    risk_level: str
    enabled: bool
    description: str = ""


class OpenWorkerBriefOut(BaseModel):
    """Worker 目录条目。"""

    name: str
    description: str = ""
    enabled: bool = True
    active_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class OpenRunFileOut(BaseModel):
    """取件清单条目（P2-1）：只列本次等待所属 run 的文件，无遍历入口。"""

    file_id: str
    name: str
    mime: str
    size: int
    # artifact = 平台产出的产物；input = 派发时随消息收下的输入附件
    source: Literal["artifact", "input"]
