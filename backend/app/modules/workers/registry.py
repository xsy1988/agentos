"""Worker 文件包注册中心：目录扫描、解析、缓存与写操作（文件为唯一权威）。

文件布局（``data/workers/<name>/``）::

    manifest.yaml            # {enabled: bool, active_version: "v2"|null(=最新)}
    v1/WORKER.md             # 主任务：yaml 头（name/description/icon/color/
                             #   capabilities/references/inputs）+ 正文 playbook
    v1/sub_workers/<子任务>/WORKER.md   # 子任务：yaml 头（seq/kind/optional/
                             #   description/capability_hint/inputs）+ 正文 playbook
    v1/references/*.md       # 主任务引用文件（WORKER.md 指定，LLM 按需加载）
    v1/tests/                # 自动测试数据（按需）
    v2/...                   # 每个版本一个文件夹，手动构建递增

纪律：
- 目录名 = Worker 唯一标识（允许中文）；WORKER.md 头 name 必须与目录名一致；
- 只有 active 版本可编辑；历史版本只读；
- 所有文件读写路径必须落在该版本目录内（防 ``../`` 越界）；
- 「通用任务」不再是文件，是代码内建常量 COMMON_WORKER（无步骤骨架）；
- front-matter 的 ``inputs``（P1-4 输入契约）在 **写入与解析两侧**都严格校验：
  未知键/非法 type/重名 一律 4xx，不允许拼错的声明被静默忽略。

缓存：per (name, version) 的 mtime 指纹缓存，写操作后主动失效；
未变更时热路径（发消息意图识别、任务卡渲染）零解析开销。
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.config import settings
from app.modules.workers.inputs import (
    InputContractError,
    InputSpec,
    dump_input_specs,
    parse_input_specs,
)

logger = logging.getLogger(__name__)

FRONT_DELIM = "---"
VERSION_RE = re.compile(r"^v(\d+)$")

# 内建「通用任务」Worker：零选择会话的兜底归属（无文件、无步骤骨架）
COMMON_WORKER = "__common__"
COMMON_WORKER_DISPLAY = "通用任务"

# 目录/标识安全约束：非空、无路径分隔符、不以点开头、长度 ≤128
_NAME_RE = re.compile(r"^.{1,128}$")


class WorkerError(ValueError):
    """Worker 文件包操作错误（router 转 4xx）。"""


# ---------- 数据结构 ----------


@dataclass
class SubWorker:
    """子任务（sub_workers/<ref>/WORKER.md 的解析结果）。"""

    ref: str  # 文件夹名（= task_steps.worker_step_ref）
    name: str
    description: str = ""
    playbook: str = ""
    seq: int = 0
    kind: str = "main"  # main | branch
    optional: bool = False
    capability_hint: list[str] = field(default_factory=list)
    # 输入契约（P1-4）：该子任务推进前需要哪些输入（声明并注入指引，门在 run 级）
    inputs: list[InputSpec] = field(default_factory=list)
    references: list[dict] | None = None


@dataclass
class WorkerDef:
    """一个 Worker 某个版本的完整解析结果。"""

    name: str  # 目录名 = 唯一标识
    version: str  # v1 / v2 …
    description: str = ""
    playbook: str = ""
    icon: str | None = None
    color: str | None = None
    capabilities: list[str] = field(default_factory=list)  # 工具引用清单（能力名）
    inputs: list[InputSpec] = field(default_factory=list)  # 输入契约（P1-4）
    references: list[dict] | None = None
    sub_workers: list[SubWorker] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name

    def ordered_sub_workers(self) -> list[SubWorker]:
        """主线在前（按 seq）、支线在后（按 seq）——实例化与任务卡的骨架顺序。"""
        return sorted(self.sub_workers, key=lambda s: (0 if s.kind == "main" else 1, s.seq))

    def main_sub_workers(self) -> list[SubWorker]:
        return [s for s in self.sub_workers if s.kind == "main"]

    def branch_sub_workers(self) -> list[SubWorker]:
        return [s for s in self.sub_workers if s.kind == "branch"]

    def find_sub(self, ref: str) -> SubWorker | None:
        for s in self.sub_workers:
            if s.ref == ref:
                return s
        return None


@dataclass
class WorkerMeta:
    """Worker 级元信息（manifest + 各版本概览）。"""

    name: str
    enabled: bool
    active_version: str | None  # None = 跟随最新版本
    versions: list[str]  # ["v1", "v2"] 升序
    def_: WorkerDef | None = None  # active（解析后）版本的定义；无版本时 None

    @property
    def latest_version(self) -> str | None:
        return self.versions[-1] if self.versions else None

    @property
    def effective_version(self) -> str | None:
        """生效版本：manifest 指定且存在 → 用之；否则最新。"""
        if self.active_version and self.active_version in self.versions:
            return self.active_version
        return self.latest_version

    @property
    def enabled_def(self) -> WorkerDef | None:
        return self.def_ if self.enabled else None


# ---------- 基础路径与解析 ----------


def workers_root() -> Path:
    root = settings.data_dir / "workers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def worker_dir(name: str) -> Path:
    return workers_root() / name


def _assert_name(name: str) -> None:
    if (
        not name
        or not _NAME_RE.match(name)
        or "/" in name
        or "\\" in name
        or name in (".", "..")
        or name.startswith(".")
    ):
        raise WorkerError(f"非法 Worker 标识：「{name}」")


def version_dir(name: str, version: str) -> Path:
    return worker_dir(name) / version


def _split_md(text: str) -> tuple[dict[str, Any], str]:
    """解析 WORKER.md：返回 (frontmatter, 正文)。无 frontmatter 视为纯正文。"""
    if not text.startswith(FRONT_DELIM):
        return {}, text
    parts = text.split(FRONT_DELIM, 2)
    if len(parts) < 3:
        return {}, text
    front = yaml.safe_load(parts[1]) or {}
    if not isinstance(front, dict):
        raise WorkerError("frontmatter 必须是 yaml 键值结构")
    return front, parts[2].strip("\n") + "\n"


def dump_md(front: dict[str, Any], body: str) -> str:
    """frontmatter + 正文 → 完整 markdown（字段序稳定，便于 diff）。"""
    body_yaml = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"{FRONT_DELIM}\n{body_yaml}{FRONT_DELIM}\n\n{body.strip()}\n"


def _parse_inputs(raw: Any, *, scope: str) -> list[InputSpec]:
    """解析 `inputs` 声明（P1-4）：契约错误统一转 `WorkerError`（router 转 4xx）。"""
    try:
        return parse_input_specs(raw, scope=scope)
    except InputContractError as e:
        raise WorkerError(str(e)) from e


def _read_manifest(name: str) -> dict[str, Any]:
    path = worker_dir(name) / "manifest.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise WorkerError(f"manifest.yaml 解析失败：{e}") from e
    return data if isinstance(data, dict) else {}


def _write_manifest(name: str, manifest: dict[str, Any]) -> None:
    path = worker_dir(name) / "manifest.yaml"
    path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def list_versions(name: str) -> list[str]:
    """该 Worker 的版本目录列表（升序 v1 < v2 < …）。"""
    _assert_name(name)
    d = worker_dir(name)
    if not d.is_dir():
        return []
    out: list[tuple[int, str]] = []
    for child in d.iterdir():
        m = VERSION_RE.match(child.name)
        if m and child.is_dir():
            out.append((int(m.group(1)), child.name))
    return [v for _, v in sorted(out)]


def list_worker_names() -> list[str]:
    """全部 Worker 目录名（含至少一个版本目录的才算；旧的 uuid 投影目录被忽略）。"""
    out: list[str] = []
    for child in workers_root().iterdir():
        if child.is_dir() and not child.name.startswith(".") and list_versions(child.name):
            out.append(child.name)
    return sorted(out)


# ---------- 缓存 ----------

# (name, version) → (指纹, WorkerDef)；指纹 = 版本目录内全部文件的 (路径, mtime_ns)
_CACHE: dict[tuple[str, str], tuple[tuple, WorkerDef]] = {}


def _dir_fingerprint(vdir: Path) -> tuple:
    marks: list[tuple[str, int]] = []
    for p in sorted(vdir.rglob("*")):
        if p.is_file():
            st = p.stat()
            marks.append((str(p.relative_to(vdir)), st.st_mtime_ns))
    return tuple(marks)


def invalidate(name: str) -> None:
    for key in [k for k in _CACHE if k[0] == name]:
        _CACHE.pop(key, None)


def _invalidate_all() -> None:
    _CACHE.clear()


# ---------- 解析（读路径） ----------


def _load_def(name: str, version: str) -> WorkerDef:
    vdir = version_dir(name, version)
    md_path = vdir / "WORKER.md"
    if not md_path.exists():
        raise WorkerError(f"Worker「{name}」的版本 {version} 缺少 WORKER.md")
    front, body = _split_md(md_path.read_text(encoding="utf-8"))

    sub_workers: list[SubWorker] = []
    subs_dir = vdir / "sub_workers"
    if subs_dir.is_dir():
        for sub in sorted(subs_dir.iterdir()):
            sub_md = sub / "WORKER.md"
            if not sub.is_dir() or not sub_md.exists():
                continue
            s_front, s_body = _split_md(sub_md.read_text(encoding="utf-8"))
            hint = s_front.get("capability_hint")
            sub_workers.append(
                SubWorker(
                    ref=sub.name,
                    name=str(s_front.get("name") or sub.name),
                    description=str(s_front.get("description") or ""),
                    playbook=s_body,
                    seq=int(s_front.get("seq") or 0),
                    kind=str(s_front.get("kind") or "main"),
                    optional=bool(s_front.get("optional") or False),
                    capability_hint=[str(x) for x in hint] if isinstance(hint, list) else [],
                    inputs=_parse_inputs(s_front.get("inputs"), scope=f"子任务「{sub.name}」"),
                    references=s_front.get("references")
                    if isinstance(s_front.get("references"), list)
                    else None,
                )
            )
    caps = front.get("capabilities")
    return WorkerDef(
        name=name,
        version=version,
        description=str(front.get("description") or ""),
        playbook=body,
        icon=str(front["icon"]) if front.get("icon") is not None else None,
        color=str(front["color"]) if front.get("color") is not None else None,
        capabilities=[str(x) for x in caps] if isinstance(caps, list) else [],
        inputs=_parse_inputs(front.get("inputs"), scope=f"Worker「{name}」"),
        references=front.get("references") if isinstance(front.get("references"), list) else None,
        sub_workers=sub_workers,
    )


def get_def(
    name: str, version: str | None = None, *, fallback_latest: bool = True
) -> WorkerDef | None:
    """取 Worker 定义（带缓存）。version 为空取生效版本。

    fallback_latest：绑定的历史版本目录已被删除时降级到最新版本（只保名称骨架，
    任务卡不至于空白）——调用方无法区分降级与否时，渲染层会因 playbook 缺失自然收敛。
    """
    _assert_name(name)
    if version and not VERSION_RE.match(version):
        return None
    if not version:
        meta = get_meta(name)
        version = meta.effective_version if meta else None
        if version is None:
            return None
    elif not version_dir(name, version).exists():
        if not fallback_latest:
            return None
        meta = get_meta(name)
        version = (meta.effective_version if meta else None) or ""
        if not version:
            return None

    vdir = version_dir(name, version)
    if not vdir.exists():
        return None
    key = (name, version)
    fp = _dir_fingerprint(vdir)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == fp:
        return cached[1]
    try:
        wdef = _load_def(name, version)
    except WorkerError:
        logger.warning("Worker 解析失败：%s@%s", name, version, exc_info=True)
        return None
    _CACHE[key] = (fp, wdef)
    return wdef


def get_meta(name: str) -> WorkerMeta | None:
    """Worker 级元信息（manifest + 版本列表 + active 版本定义）。"""
    _assert_name(name)
    versions = list_versions(name)
    if not versions:
        return None
    manifest = _read_manifest(name)
    active = manifest.get("active_version")
    meta = WorkerMeta(
        name=name,
        enabled=bool(manifest.get("enabled", True)),
        active_version=str(active) if active else None,
        versions=versions,
    )
    if meta.effective_version:
        meta.def_ = get_def(name, meta.effective_version)
    return meta


def list_workers() -> list[WorkerMeta]:
    """全部 Worker 元信息（列表页数据源）。"""
    return [m for m in (get_meta(n) for n in list_worker_names()) if m is not None]


def iter_enabled_worker_defs() -> list[WorkerDef]:
    """全部启用 Worker 的生效版本定义（意图识别 / 能力域全量计算用）。"""
    out: list[WorkerDef] = []
    for meta in list_workers():
        if meta.enabled and meta.def_ is not None:
            out.append(meta.def_)
    return out


def referenced_capability_names() -> set[str]:
    """全部启用 Worker（生效版本）引用的能力名集合（「通用兜底档」的反面）。"""
    names: set[str] = set()
    for wdef in iter_enabled_worker_defs():
        names.update(wdef.capabilities)
    return names


# ---------- 写操作（脚手架 / 版本 / 启停 / 删除） ----------


def _dump_main_md(
    name: str,
    description: str,
    icon: str | None,
    color: str | None,
    capabilities: list[str] | None,
    references: list[dict] | None,
    inputs: list[InputSpec] | None = None,
) -> str:
    front: dict[str, Any] = {"name": name, "description": description}
    if icon:
        front["icon"] = icon
    if color:
        front["color"] = color
    front["capabilities"] = capabilities or []
    if inputs:
        front["inputs"] = dump_input_specs(inputs)
    if references:
        front["references"] = references
    body = (
        f"# {name}\n\n"
        "## 干什么\n（目标与产出：一句话讲清这个 Worker 处理什么场景）\n\n"
        "## 第零步：依赖预检（必做）\n"
        "（依赖哪些服务 / API / 数据源，如何探测（优先 probe_url）；"
        "不通时告知用户「依赖未就绪」并停止，不臆造数据）\n\n"
        "## 怎么干\n1. （主线步骤…）\n\n"
        "## 会遇到什么问题 + 如何处理\n"
        "- （问题描述）→ 启用子 Worker：sub_workers/<子任务名>\n\n"
        "## 工具引用清单与能力路由表\n"
        "- 工具名（read/write）：用途与用法\n"
    )
    return dump_md(front, body)


def _dump_sub_md(
    name: str,
    seq: int,
    kind: str,
    description: str,
    inputs: list[InputSpec] | None = None,
) -> str:
    front: dict[str, Any] = {
        "name": name,
        "seq": seq,
        "kind": kind,
        "optional": kind == "branch",
        "description": description,
        "capability_hint": [],
    }
    if inputs:
        front["inputs"] = dump_input_specs(inputs)
    body = f"# {name}\n\n（这一步怎么干、遇到什么问题、调哪个能力）\n"
    return dump_md(front, body)


def create_worker(
    name: str,
    *,
    description: str = "",
    icon: str | None = None,
    color: str | None = None,
    capabilities: list[str] | None = None,
    references: list[dict] | None = None,
    inputs: list[dict] | None = None,
) -> WorkerMeta:
    """新建 Worker 文件包脚手架（v1 + manifest）。已存在则报错。"""
    _assert_name(name)
    d = worker_dir(name)
    if d.exists():
        raise WorkerError(f"Worker「{name}」已存在")
    specs = _parse_inputs(inputs, scope=f"Worker「{name}」")
    vdir = d / "v1"
    (vdir / "sub_workers").mkdir(parents=True)
    (vdir / "references").mkdir()
    (vdir / "WORKER.md").write_text(
        _dump_main_md(name, description, icon, color, capabilities, references, specs),
        encoding="utf-8",
    )
    _write_manifest(name, {"enabled": True})
    invalidate(name)
    logger.info("Worker 文件包已创建：%s", d)
    meta = get_meta(name)
    assert meta is not None
    return meta


def _write_sub_workers(vdir: Path, sub_workers: list[dict[str, Any]] | None) -> None:
    """把子任务清单逐个写成 sub_workers/<名>/WORKER.md。"""
    for st in sub_workers or []:
        synced = _parse_inputs(st.get("inputs"), scope=f"子任务「{st['name']}」")
        sub_dir = vdir / "sub_workers" / str(st["name"])
        sub_dir.mkdir(exist_ok=True)
        content = dump_md(
            {
                "name": st["name"],
                "seq": st.get("seq", 0),
                "kind": st.get("kind", "main"),
                "optional": bool(st.get("optional", False)),
                "description": st.get("description", ""),
                "capability_hint": st.get("capability_hint") or [],
                **({"inputs": dump_input_specs(synced)} if synced else {}),
            },
            st.get("playbook", ""),
        )
        rel = f"sub_workers/{st['name']}/WORKER.md"
        _validate_worker_md(str(vdir.parent.name), rel, content)
        sub_dir.joinpath("WORKER.md").write_text(content, encoding="utf-8")


def _dump_worker_md(
    name: str,
    description: str,
    icon: str | None,
    color: str | None,
    capabilities: list[str] | None,
    references: list[dict] | None,
    playbook: str,
    inputs: list[InputSpec] | None = None,
) -> str:
    """主 WORKER.md：有 playbook 用正文，无则落脚手架模板。"""
    if playbook:
        front: dict[str, Any] = {"name": name, "description": description}
        if icon:
            front["icon"] = icon
        if color:
            front["color"] = color
        front["capabilities"] = capabilities or []
        if inputs:
            front["inputs"] = dump_input_specs(inputs)
        if references:
            front["references"] = references
        return dump_md(front, playbook)
    return _dump_main_md(name, description, icon, color, capabilities, references, inputs)


def ensure_worker(
    name: str,
    *,
    description: str = "",
    icon: str | None = None,
    color: str | None = None,
    capabilities: list[str] | None = None,
    references: list[dict] | None = None,
    playbook: str = "",
    sub_workers: list[dict[str, Any]] | None = None,
    inputs: list[dict] | None = None,
) -> WorkerMeta:
    """幂等 seed：不存在则按给定内容创建（已存在不覆盖，保护用户编辑）。"""
    meta = get_meta(name)
    if meta is not None:
        return meta
    _assert_name(name)
    specs = _parse_inputs(inputs, scope=f"Worker「{name}」")
    d = worker_dir(name)
    vdir = d / "v1"
    (vdir / "sub_workers").mkdir(parents=True)
    (vdir / "references").mkdir()
    (vdir / "WORKER.md").write_text(
        _dump_worker_md(name, description, icon, color, capabilities, references, playbook, specs),
        encoding="utf-8",
    )
    _write_sub_workers(vdir, sub_workers)
    _write_manifest(name, {"enabled": True})
    invalidate(name)
    logger.info("Worker 文件包已 seed：%s", d)
    meta = get_meta(name)
    assert meta is not None
    return meta


def publish_version(
    name: str,
    *,
    description: str = "",
    icon: str | None = None,
    color: str | None = None,
    capabilities: list[str] | None = None,
    references: list[dict] | None = None,
    playbook: str = "",
    sub_workers: list[dict[str, Any]] | None = None,
    inputs: list[dict] | None = None,
) -> str:
    """基于当前生效版本构建 vN+1，并整体覆写为新内容（开放 API 演进注册用）。

    与 ensure_worker 的区别：ensure_worker 只在「不存在」时落 v1（幂等保护）；
    publish_version 面向已存在 Worker 的版本演进——历史版本原样保留（版本纪律），
    新版本主 WORKER.md 与 sub_workers 以本次提交为准（旧子任务目录清空重建，
    references/ 与 tests/ 文件原样继承）。
    """
    meta = get_meta(name)
    if meta is None:
        raise WorkerError(f"Worker「{name}」不存在")
    if meta.effective_version is None:
        raise WorkerError(f"Worker「{name}」没有可复制的版本")
    specs = _parse_inputs(inputs, scope=f"Worker「{name}」")
    version = build_version(name)
    vdir = version_dir(name, version)
    (vdir / "WORKER.md").write_text(
        _dump_worker_md(name, description, icon, color, capabilities, references, playbook, specs),
        encoding="utf-8",
    )
    shutil.rmtree(vdir / "sub_workers", ignore_errors=True)
    (vdir / "sub_workers").mkdir(parents=True)
    _write_sub_workers(vdir, sub_workers)
    invalidate(name)
    logger.info("Worker「%s」已发布新版本 %s（开放 API）", name, version)
    return version


def build_version(name: str) -> str:
    """手动构建新版本：复制当前生效版本目录 → vN+1，并把 active 指向新版本。"""
    meta = get_meta(name)
    if meta is None:
        raise WorkerError(f"Worker「{name}」不存在")
    if meta.effective_version is None:
        raise WorkerError(f"Worker「{name}」没有可复制的版本")
    latest_no = int(meta.latest_version.lstrip("v"))  # type: ignore[union-attr]
    new_version = f"v{latest_no + 1}"
    src = version_dir(name, meta.effective_version)
    dst = version_dir(name, new_version)
    if dst.exists():
        raise WorkerError(f"版本 {new_version} 已存在")
    shutil.copytree(src, dst)
    manifest = _read_manifest(name)
    manifest["active_version"] = new_version
    _write_manifest(name, manifest)
    invalidate(name)
    logger.info("Worker「%s」构建新版本 %s（复制自 %s）", name, new_version, meta.effective_version)
    return new_version


def delete_version(name: str, version: str) -> None:
    meta = get_meta(name)
    if meta is None:
        raise WorkerError(f"Worker「{name}」不存在")
    if version not in meta.versions:
        raise WorkerError(f"版本 {version} 不存在")
    if len(meta.versions) <= 1:
        raise WorkerError("至少保留一个版本（删除整个 Worker 请用删除入口）")
    if version == meta.effective_version:
        raise WorkerError("不能删除当前启用版本（先切换 active_version）")
    shutil.rmtree(version_dir(name, version))
    invalidate(name)


def set_enabled(name: str, enabled: bool) -> WorkerMeta:
    meta = get_meta(name)
    if meta is None:
        raise WorkerError(f"Worker「{name}」不存在")
    manifest = _read_manifest(name)
    manifest["enabled"] = bool(enabled)
    _write_manifest(name, manifest)
    invalidate(name)
    out = get_meta(name)
    assert out is not None
    return out


def set_active_version(name: str, version: str) -> WorkerMeta:
    meta = get_meta(name)
    if meta is None:
        raise WorkerError(f"Worker「{name}」不存在")
    if version not in meta.versions:
        raise WorkerError(f"版本 {version} 不存在")
    manifest = _read_manifest(name)
    manifest["active_version"] = version
    _write_manifest(name, manifest)
    invalidate(name)
    out = get_meta(name)
    assert out is not None
    return out


def delete_worker(name: str) -> None:
    meta = get_meta(name)
    if meta is None:
        raise WorkerError(f"Worker「{name}」不存在")
    shutil.rmtree(worker_dir(name))
    invalidate(name)


# ---------- 版本内文件操作（路径安全 + 只写 active） ----------


def _resolve_safe(name: str, version: str, rel_path: str) -> Path:
    """把相对路径解析到版本目录内的绝对路径，防越界。"""
    if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
        raise WorkerError(f"非法文件路径：「{rel_path}」")
    vdir = version_dir(name, version).resolve()
    target = (vdir / rel_path).resolve()
    if target != vdir and vdir not in target.parents:
        raise WorkerError(f"非法文件路径：「{rel_path}」")
    return target


def assert_version_writable(name: str, version: str) -> None:
    """只有当前生效版本可写（历史版本只读）。"""
    meta = get_meta(name)
    if meta is None:
        raise WorkerError(f"Worker「{name}」不存在")
    if version not in meta.versions:
        raise WorkerError(f"版本 {version} 不存在")
    if version != meta.effective_version:
        raise WorkerError(f"版本 {version} 是历史版本，只读（请先切换 active 或构建新版本）")


def read_version_file(name: str, version: str, rel_path: str) -> str:
    target = _resolve_safe(name, version, rel_path)
    if not target.is_file():
        raise WorkerError(f"文件不存在：{rel_path}")
    return target.read_text(encoding="utf-8")


def _validate_worker_md(name: str, rel_path: str, content: str) -> None:
    """WORKER.md 写入校验：frontmatter 可解析 + name 与目录/文件夹一致 + inputs 合法。"""
    parts = rel_path.split("/")
    front, _ = _split_md(content)
    if parts == ["WORKER.md"]:
        if front.get("name") and str(front["name"]).strip() != name:
            raise WorkerError(
                f"WORKER.md 头 name「{front['name']}」与 Worker「{name}」不一致"
                "（重命名 = 新建 Worker）"
            )
        _parse_inputs(front.get("inputs"), scope=f"Worker「{name}」")
    elif len(parts) == 3 and parts[0] == "sub_workers" and parts[2] == "WORKER.md":
        if front.get("name") and str(front["name"]).strip() != parts[1]:
            raise WorkerError(
                f"子任务 WORKER.md 头 name「{front['name']}」与文件夹「{parts[1]}」不一致"
            )
        if front.get("kind") not in (None, "main", "branch"):
            raise WorkerError(f"子任务 kind 只能是 main/branch，得到「{front.get('kind')}」")
        _parse_inputs(front.get("inputs"), scope=f"子任务「{parts[1]}」")


def write_version_file(name: str, version: str, rel_path: str, content: str) -> None:
    assert_version_writable(name, version)
    target = _resolve_safe(name, version, rel_path)
    if rel_path.endswith(".md") or rel_path.endswith("WORKER.md"):
        _validate_worker_md(name, rel_path, content)
    suffix = target.suffix.lower()
    if suffix in ("", ".md", ".yaml", ".yml", ".txt", ".json", ".py"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            raise WorkerError(f"写入失败：{e}") from e
    else:
        raise WorkerError(f"不支持的文件类型：{suffix}（仅文本文件）")
    invalidate(name)


def create_version_file(name: str, version: str, rel_path: str, content: str) -> None:
    assert_version_writable(name, version)
    target = _resolve_safe(name, version, rel_path)
    if target.exists():
        raise WorkerError(f"文件已存在：{rel_path}")
    if rel_path.endswith("WORKER.md"):
        _validate_worker_md(name, rel_path, content)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    invalidate(name)


def delete_version_file(name: str, version: str, rel_path: str) -> None:
    assert_version_writable(name, version)
    if rel_path in ("WORKER.md", "sub_workers", "references", "tests"):
        raise WorkerError("WORKER.md 与顶层目录不可删除")
    target = _resolve_safe(name, version, rel_path)
    if not target.exists():
        raise WorkerError(f"文件不存在：{rel_path}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    invalidate(name)


def create_sub_worker(
    name: str,
    version: str,
    sub_name: str,
    *,
    kind: str = "main",
    inputs: list[dict] | None = None,
) -> Path:
    """新建子任务文件夹（sub_workers/<名>/WORKER.md 脚手架）。"""
    assert_version_writable(name, version)
    if not sub_name or "/" in sub_name or sub_name in (".", "..") or sub_name.startswith("."):
        raise WorkerError(f"非法子任务名：「{sub_name}」")
    wdef = get_def(name, version)
    if wdef is None:
        raise WorkerError(f"Worker「{name}」版本 {version} 解析失败")
    if wdef.find_sub(sub_name) is not None:
        raise WorkerError(f"子任务「{sub_name}」已存在")
    seq = max((s.seq for s in wdef.sub_workers), default=0) + 1
    specs = _parse_inputs(inputs, scope=f"子任务「{sub_name}」")
    content = _dump_sub_md(sub_name, seq, kind, "", specs)
    _validate_worker_md(name, f"sub_workers/{sub_name}/WORKER.md", content)
    sub_dir = version_dir(name, version) / "sub_workers" / sub_name
    sub_dir.mkdir(parents=True, exist_ok=True)
    sub_dir.joinpath("WORKER.md").write_text(content, encoding="utf-8")
    invalidate(name)
    return sub_dir


# ---------- 文件树（在线文件管理器） ----------


def version_tree(name: str, version: str) -> dict[str, Any]:
    """版本目录树（目录优先、文件名字典序）。"""
    vdir = version_dir(name, version)
    if not vdir.is_dir():
        raise WorkerError(f"版本 {version} 不存在")

    def build(d: Path) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        for child in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.is_dir():
                children.append({"name": child.name, "type": "dir", "children": build(child)})
            else:
                children.append({"name": child.name, "type": "file"})
        return children

    return {"name": version, "type": "dir", "children": build(vdir)}
