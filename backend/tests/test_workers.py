"""workers registry 纯文件逻辑单测（tmp 目录，不依赖 DB / LLM）。

覆盖：WORKER.md 解析、mtime 缓存、版本构建/删除、路径越界防护、
历史版本只读、引用清单（capabilities）聚合、通用 Worker 常量语义。
"""

from pathlib import Path

import pytest

from app.modules.workers import registry
from app.modules.workers.registry import (
    COMMON_WORKER,
    WorkerError,
)


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 registry 的文件根指到临时目录，测试互不污染。"""
    monkeypatch.setattr(registry.settings, "data_dir", tmp_path)
    registry._invalidate_all()
    yield tmp_path
    registry._invalidate_all()


def _mk_worker(root: Path, name: str = "报价对比", capabilities: list[str] | None = None) -> None:
    registry.create_worker(
        name,
        description="对比供应商报价",
        icon="📊",
        color="#1677ff",
        capabilities=capabilities or ["web_search"],
    )
    # create_worker 只给主 md 脚手架；补一个子任务便于覆盖解析
    registry.write_version_file(
        name,
        "v1",
        "sub_workers/拉取报价/WORKER.md",
        (
            "---\n"
            "name: 拉取报价\n"
            "seq: 1\n"
            "kind: main\n"
            "optional: false\n"
            "description: 抓取各供应商报价单\n"
            "capability_hint:\n"
            "  - web_search\n"
            "---\n\n"
            "# 拉取报价\n\n调 web_search 抓报价。\n"
        ),
    )


def test_parse_and_cache(root: Path) -> None:
    _mk_worker(root)
    wdef = registry.get_def("报价对比", "v1")
    assert wdef is not None
    assert wdef.description == "对比供应商报价"
    assert wdef.icon == "📊"
    assert wdef.capabilities == ["web_search"]
    assert [s.ref for s in wdef.ordered_sub_workers()] == ["拉取报价"]
    sub = wdef.find_sub("拉取报价")
    assert sub is not None and sub.capability_hint == ["web_search"] and sub.seq == 1

    # mtime 未变 → 命中缓存（同一对象）
    assert registry.get_def("报价对比", "v1") is wdef


def test_list_and_enabled(root: Path) -> None:
    _mk_worker(root)
    metas = registry.list_workers()
    assert [m.name for m in metas] == ["报价对比"]
    assert metas[0].enabled and metas[0].versions == ["v1"]

    registry.set_enabled("报价对比", False)
    assert registry.list_workers()[0].enabled is False
    assert registry.iter_enabled_worker_defs() == []
    assert registry.referenced_capability_names() == set()


def test_referenced_capability_names(root: Path) -> None:
    _mk_worker(root, capabilities=["web_search", "parse_quote"])
    assert registry.referenced_capability_names() == {"web_search", "parse_quote"}


def test_build_version_and_history_readonly(root: Path) -> None:
    _mk_worker(root)
    v2 = registry.build_version("报价对比")
    assert v2 == "v2"
    meta = registry.list_workers()[0]
    assert meta.versions == ["v1", "v2"] and meta.effective_version == "v2"

    # 历史版本只读：写 v1 报错，写 v2 成功
    with pytest.raises(WorkerError, match="只读"):
        registry.write_version_file("报价对比", "v1", "references/x.md", "# x\n")
    registry.write_version_file("报价对比", "v2", "references/x.md", "# x\n")
    # v2 的编辑不影响 v1（版本隔离）
    assert registry.read_version_file("报价对比", "v1", "WORKER.md") != ""


def test_delete_version_guards(root: Path) -> None:
    _mk_worker(root)
    with pytest.raises(WorkerError, match="至少保留一个版本"):
        registry.delete_version("报价对比", "v1")
    registry.build_version("报价对比")
    with pytest.raises(WorkerError, match="不能删除当前启用版本"):
        registry.delete_version("报价对比", "v2")
    registry.delete_version("报价对比", "v1")
    assert registry.list_workers()[0].versions == ["v2"]


def test_path_traversal_blocked(root: Path) -> None:
    _mk_worker(root)
    with pytest.raises(WorkerError):
        registry.read_version_file("报价对比", "v1", "../../etc/passwd")
    with pytest.raises(WorkerError):
        registry.write_version_file("报价对比", "v1", "sub_workers/../../x.md", "# x\n")
    # 绝对路径同样拒绝
    with pytest.raises(WorkerError):
        registry.read_version_file("报价对比", "v1", "/etc/passwd")


def test_worker_md_name_consistency(root: Path) -> None:
    _mk_worker(root)
    with pytest.raises(WorkerError, match="不一致"):
        registry.write_version_file(
            "报价对比", "v1", "WORKER.md", "---\nname: 别的名字\n---\n\n正文\n"
        )


def test_get_def_fallback_to_latest(root: Path) -> None:
    """绑定版本目录被删时降级最新版本，任务卡不至于空白。"""
    _mk_worker(root)
    registry.build_version("报价对比")  # active → v2，删掉 v1
    import shutil

    shutil.rmtree(registry.version_dir("报价对比", "v1"))
    registry.invalidate("报价对比")
    wdef = registry.get_def("报价对比", "v1")
    assert wdef is not None and wdef.version == "v2"
    assert registry.get_def("报价对比", "v1", fallback_latest=False) is None


def test_common_worker_semantics(root: Path) -> None:
    """通用任务不落文件：目录不存在，get_meta 为 None。"""
    assert COMMON_WORKER == "__common__"
    assert registry.get_meta(COMMON_WORKER) is None
    assert registry.get_def(COMMON_WORKER) is None


def test_ensure_worker_idempotent(root: Path) -> None:
    registry.ensure_worker(
        "采购",
        description="d1",
        playbook="# v1 内容\n",
        sub_workers=[{"name": "确认需求", "seq": 1, "kind": "main", "playbook": "# 步骤\n"}],
    )
    # 第二次不覆盖（保护用户编辑）
    registry.ensure_worker("采购", description="d2", playbook="# 被忽略\n")
    wdef = registry.get_def("采购")
    assert wdef is not None and wdef.description == "d1"
    assert wdef.playbook.startswith("# v1 内容")
    assert [s.ref for s in wdef.sub_workers] == ["确认需求"]


def test_version_tree_and_create_sub_worker(root: Path) -> None:
    _mk_worker(root)
    tree = registry.version_tree("报价对比", "v1")
    names = {c["name"] for c in tree["children"]}
    assert {"WORKER.md", "sub_workers", "references"} <= names

    registry.create_sub_worker("报价对比", "v1", "对比分析", kind="branch")
    wdef = registry.get_def("报价对比", "v1")
    assert wdef is not None
    sub = wdef.find_sub("对比分析")
    assert sub is not None and sub.kind == "branch" and sub.optional is True
