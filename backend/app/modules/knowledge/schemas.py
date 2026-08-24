"""knowledge 入参/出参。"""

from pydantic import BaseModel, Field


class FolderCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_id: str | None = None
    description: str | None = None
    sort_order: int = 0


class FolderUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: str | None = None  # 传值=移动；None=不移动（与"移到顶层"区分用 "" ）
    description: str | None = None


class FolderOut(BaseModel):
    id: str
    name: str
    parent_id: str | None
    path: str
    description: str | None
    sort_order: int
    doc_count: int = 0


class DocCreateIn(BaseModel):
    file_id: str
    folder_id: str
    title: str | None = None  # 缺省用文件名


class DocRetryIn(BaseModel):
    """断点重试：从当前 status 所处步骤继续。"""


class DocOut(BaseModel):
    id: str
    folder_id: str
    title: str
    source_file_id: str | None
    source_type: str
    status: str
    error: str | None
    chunk_count: int
    embedding_model: str | None


class SearchIn(BaseModel):
    """检索测试器入参（与 search_knowledge 工具同构）。"""

    query: str = Field(min_length=1)
    folders: list[str] = Field(default_factory=list)  # path 前缀圈定范围，空=全库
    k: int = Field(default=5, ge=1, le=20)


class SearchHit(BaseModel):
    doc_id: str
    doc_title: str
    folder_path: str
    heading_path: str | None
    content: str
    score: float
