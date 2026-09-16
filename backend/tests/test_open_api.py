"""open_api 单测：publish_version 版本演进 + 令牌鉴权 + 第三方 tool 守卫 + 入参校验。

不依赖 DB / LLM：registry 用 tmp 目录；HTTP 层只测鉴权依赖（503/401 在进 DB 前拦截）。
"""

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.modules.capabilities.schemas import CapabilityCreateIn
from app.modules.open_api.schemas import (
    OpenSubWorkerIn,
    OpenWorkerIn,
    OpenWorkerRegisterIn,
)
from app.modules.open_api.service import _guard_third_party_tool, _normalize_sub_workers
from app.modules.workers import registry

client = TestClient(app)


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 registry 的文件根指到临时目录，测试互不污染。"""
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    yield tmp_path
    registry._invalidate_all()


# ---------- registry.publish_version（开放 API 演进注册的落盘核心） ----------


def test_publish_version_creates_new_version_and_rebuilds_subs(root: Path) -> None:
    registry.ensure_worker(
        "爬虫报告",
        description="v1 描述",
        playbook="# v1\n",
        sub_workers=[{"name": "旧步骤", "seq": 1, "kind": "main", "playbook": "# 旧\n"}],
    )
    v2 = registry.publish_version(
        "爬虫报告",
        description="v2 描述",
        capabilities=["legal_crawl_status"],
        playbook="# v2\n",
        sub_workers=[
            {"name": "新步骤A", "seq": 1, "kind": "main", "playbook": "# A\n"},
            {"name": "确认纠错", "seq": 2, "kind": "branch", "description": "支线"},
        ],
    )
    assert v2 == "v2"
    meta = registry.get_meta("爬虫报告")
    assert meta is not None
    assert meta.versions == ["v1", "v2"] and meta.effective_version == "v2"

    # 新版本内容以本次提交为准（子任务清空重建，不含旧步骤）
    wdef = registry.get_def("爬虫报告", "v2")
    assert wdef is not None
    assert wdef.description == "v2 描述"
    assert wdef.capabilities == ["legal_crawl_status"]
    assert wdef.playbook == "# v2\n"
    assert [s.ref for s in wdef.ordered_sub_workers()] == ["新步骤A", "确认纠错"]

    # 历史版本原样保留（版本纪律）
    old = registry.get_def("爬虫报告", "v1")
    assert old is not None and old.description == "v1 描述"
    assert [s.ref for s in old.sub_workers] == ["旧步骤"]


def test_publish_version_requires_existing_worker(root: Path) -> None:
    with pytest.raises(registry.WorkerError, match="不存在"):
        registry.publish_version("不存在的", playbook="# x\n")


# ---------- 令牌鉴权（HTTP 层，进 DB 前拦截） ----------


def test_open_api_disabled_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "open_api_token", None)
    resp = client.get("/api/v1/open/workers")
    assert resp.status_code == 503


def test_open_api_rejects_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "open_api_token", "secret-token")
    resp = client.get("/api/v1/open/workers", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
    resp = client.get("/api/v1/open/workers")
    assert resp.status_code == 401


# ---------- 第三方 tool 守卫 ----------


def test_guard_rejects_schema_only_tool() -> None:
    """纯 schema 声明的 tool 没有引擎执行通道，必须引导走 mcp/plugin。"""
    cap = CapabilityCreateIn(
        type="tool",
        name="my_tool",
        description="d",
        payload={"schema": {"type": "object", "properties": {}}},
    )
    with pytest.raises(HTTPException) as ei:
        _guard_third_party_tool(cap)
    assert ei.value.status_code == 422
    assert "mcp" in str(ei.value.detail)


def test_guard_allows_builtin_reference() -> None:
    cap = CapabilityCreateIn(
        type="tool",
        name="probe_ref",
        description="d",
        payload={"builtin": "probe_url", "schema": {"type": "object", "properties": {}}},
    )
    _guard_third_party_tool(cap)  # 不抛


# ---------- 入参校验 / 归一化 ----------


def test_worker_name_rejects_path_chars() -> None:
    with pytest.raises(ValueError, match="路径分隔符"):
        OpenWorkerIn(name="a/b", description="d")
    with pytest.raises(ValueError, match="路径分隔符"):
        OpenWorkerIn(name=".hidden", description="d")


def test_if_exists_literal() -> None:
    body = {"worker": {"name": "x", "description": "d"}}
    with pytest.raises(ValueError):
        OpenWorkerRegisterIn(**body, if_exists="overwrite")  # type: ignore[arg-type]
    ok = OpenWorkerRegisterIn(**body)
    assert ok.if_exists == "fail"


def test_normalize_sub_workers_optional_inference() -> None:
    subs = _normalize_sub_workers(
        [
            OpenSubWorkerIn(name="主线步", seq=1, kind="main"),
            OpenSubWorkerIn(name="支线步", seq=2, kind="branch"),
        ]
    )
    assert subs[0]["optional"] is False  # main 缺省 false
    assert subs[1]["optional"] is True  # branch 缺省 true
