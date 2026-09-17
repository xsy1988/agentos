"""外部服务取件通道（方案 §5 P2-1）。

红线：外部服务**只能**取到自己那笔等待所属 run 的文件，且没有任何可遍历的入口。
覆盖四层：

1. 鉴权层（HTTP）：`X-API-Key`（平台级）+ `callback_token`（等待级）双闸，缺一即拒；
2. 归属层（service）：清单只含该 run 的产物 + 输入附件；他人 run 的文件一律取不到；
3. 通道层（HTTP）：下载可读、Content-Disposition 为附件、超上限 413、内容缺失 404；
4. 派发契约：`await_callback` 带上取件模板 `files_url`（上下文通道与 args 历史通道一致）。
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.modules.awaits.models import AwaitBroker
from app.modules.awaits.service import make_callback_token, pickup_url_template
from app.modules.engine import tools_builtin
from app.modules.engine.tool_context import ToolContext
from app.modules.files.models import File
from app.modules.open_api import files as open_files
from app.modules.runs.models import Run, RunArtifact

client = TestClient(app)
TOKEN = "open-api-secret"


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """文件落盘根指到临时目录；开放令牌配好；结束后清依赖覆盖。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "open_api_token", TOKEN)
    yield tmp_path
    app.dependency_overrides.clear()


# ---------- 测试替身 ----------


class _Rows:
    """`ScalarResult` 的最小替身：可迭代 + `all()`（`run_artifacts` 是流式消费的）。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)

    def all(self) -> list:
        return list(self._rows)


class _FakeDB:
    """按表名路由的最小替身：只实现取件通道用到的三个读操作。"""

    def __init__(
        self,
        *,
        await_row: AwaitBroker | None = None,
        run: Run | None = None,
        artifacts: list[RunArtifact] | None = None,
        files: list[File] | None = None,
    ) -> None:
        self.await_row = await_row
        self.run = run
        self.artifacts = artifacts or []
        self.files = files or []

    async def get(self, model: type, pk: object) -> object | None:
        if model is AwaitBroker:
            if self.await_row is None or self.await_row.id != pk:
                return None
            return self.await_row
        if model is Run:
            return self.run if self.run is not None and self.run.id == pk else None
        if model is File:
            for f in self.files:
                if f.id == pk:
                    return f
            return None
        return None

    async def scalars(self, stmt: object) -> _Rows:
        text = str(stmt)
        if "run_artifacts" in text:
            return _Rows(self.artifacts)
        if "files" in text:
            return _Rows(self.files)
        return _Rows([])


def _use_db(fake: _FakeDB) -> None:
    async def _override():  # type: ignore[no-untyped-def]
        yield fake

    app.dependency_overrides[get_db] = _override


def _await_row(run_id: UUID | None = None) -> AwaitBroker:
    return AwaitBroker(
        id=uuid4(),
        run_id=run_id or uuid4(),
        tool_name="procurement_trigger",
        idempotency_key="k" * 32,
        callback_token="t" * 32,
        status="waiting",
    )


def _file(tmp_path: Path, name: str, content: bytes = b"quote,price\n") -> File:
    rel = Path("data/files") / name
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)
    return File(
        id=uuid4(),
        path=str(rel),
        filename=name,
        mime="text/csv",
        size=len(content),
        sha256="0" * 64,
    )


def _artifact(run_id: UUID, file_id: UUID, name: str = "比价结果") -> RunArtifact:
    return RunArtifact(id=uuid4(), run_id=run_id, kind="file", name=name, size=17, file_id=file_id)


# ---------- 1. 鉴权层 ----------


def test_files_channel_requires_api_key(root: Path) -> None:
    await_id = uuid4()
    _use_db(_FakeDB())
    assert client.get(f"/api/v1/open/awaits/{await_id}/files").status_code == 401
    assert (
        client.get(f"/api/v1/open/files/{uuid4()}/content?await_id={await_id}").status_code == 401
    )


def test_files_channel_disabled_without_platform_token(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "open_api_token", None)
    resp = client.get(f"/api/v1/open/awaits/{uuid4()}/files", headers={"X-API-Key": TOKEN})
    assert resp.status_code == 503


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_files_channel_requires_await_credential(root: Path, token: str | None) -> None:
    """平台级 X-API-Key 不够：还必须有这笔等待的 callback_token（403 在查库前拦）。"""
    await_id = uuid4()
    _use_db(_FakeDB(await_row=_await_row()))
    headers = {"X-API-Key": TOKEN}
    if token is not None:
        headers["X-Callback-Token"] = token
    resp = client.get(f"/api/v1/open/awaits/{await_id}/files", headers=headers)
    assert resp.status_code == 403


def test_files_channel_unknown_await_is_404(root: Path) -> None:
    await_id = uuid4()
    _use_db(_FakeDB(await_row=None))
    resp = client.get(
        f"/api/v1/open/awaits/{await_id}/files",
        headers={"X-API-Key": TOKEN, "X-Callback-Token": make_callback_token(str(await_id))},
    )
    assert resp.status_code == 404


# ---------- 2. 归属层 ----------


def test_list_run_files_empty_when_nothing_registered(root: Path) -> None:
    run = Run(id=uuid4(), agent_id=uuid4(), trigger="manual", input={})
    assert asyncio.run(open_files.list_run_files(_FakeDB(run=run), run.id)) == []  # type: ignore[arg-type]


def test_list_run_files_includes_input_attachments(root: Path) -> None:
    att = _file(root, "report.pdf")
    run = Run(
        id=uuid4(),
        agent_id=uuid4(),
        trigger="manual",
        input={"attachment_ids": [str(att.id), ""]},
    )
    items = asyncio.run(
        open_files.list_run_files(_FakeDB(run=run, files=[att]), run.id)  # type: ignore[arg-type]
    )
    assert [(i["file_id"], i["source"]) for i in items] == [(str(att.id), "input")]


def test_list_run_files_ignores_non_list_attachment_ids(root: Path) -> None:
    """`attachment_ids` 被污染成非列表时不炸、也不放行任何文件。"""
    run = Run(id=uuid4(), agent_id=uuid4(), trigger="manual", input={"attachment_ids": "abc"})
    assert asyncio.run(open_files.list_run_files(_FakeDB(run=run), run.id)) == []  # type: ignore[arg-type]


def test_get_run_file_rejects_foreign_file(root: Path) -> None:
    run = Run(id=uuid4(), agent_id=uuid4(), trigger="manual", input={})
    mine = _file(root, "mine.csv")
    theirs = _file(root, "theirs.csv")
    db = _FakeDB(run=run, artifacts=[_artifact(run.id, mine.id)], files=[mine, theirs])
    assert asyncio.run(open_files.get_run_file(db, run.id, mine.id)) is mine  # type: ignore[arg-type]
    # 不归本 run 的文件 → None（调用方统一转 404，不给存在性预言）
    assert asyncio.run(open_files.get_run_file(db, run.id, theirs.id)) is None  # type: ignore[arg-type]


def test_await_run_id_resolves_scope(root: Path) -> None:
    row = _await_row()
    db = _FakeDB(await_row=row)
    assert asyncio.run(open_files.await_run_id(db, row.id)) == row.run_id  # type: ignore[arg-type]
    assert asyncio.run(open_files.await_run_id(_FakeDB(), row.id)) is None  # type: ignore[arg-type]


# ---------- 3. 通道层（HTTP） ----------


def _headers(await_id: UUID) -> dict[str, str]:
    return {
        "X-API-Key": TOKEN,
        "X-Callback-Token": make_callback_token(str(await_id)),
    }


def test_list_await_files_returns_artifact_and_input(root: Path) -> None:
    row = _await_row()
    att = _file(root, "quote.xlsx")
    artifact_file = _file(root, "result.csv")
    run = Run(
        id=row.run_id, agent_id=uuid4(), trigger="manual", input={"attachment_ids": [str(att.id)]}
    )
    _use_db(
        _FakeDB(
            await_row=row,
            run=run,
            artifacts=[_artifact(row.run_id, artifact_file.id)],
            files=[att],
        )
    )
    resp = client.get(f"/api/v1/open/awaits/{row.id}/files", headers=_headers(row.id))
    assert resp.status_code == 200
    body = resp.json()
    assert {(i["file_id"], i["source"]) for i in body} == {
        (str(artifact_file.id), "artifact"),
        (str(att.id), "input"),
    }


def test_download_own_artifact(root: Path) -> None:
    row = _await_row()
    f = _file(root, "result.csv", b"a,b\n1,2\n")
    run = Run(id=row.run_id, agent_id=uuid4(), trigger="manual", input={})
    _use_db(_FakeDB(await_row=row, run=run, artifacts=[_artifact(row.run_id, f.id)], files=[f]))
    resp = client.get(
        f"/api/v1/open/files/{f.id}/content?await_id={row.id}", headers=_headers(row.id)
    )
    assert resp.status_code == 200
    assert resp.content == b"a,b\n1,2\n"
    assert resp.headers["content-type"].startswith("text/csv")
    # 取件是下载而非内联渲染
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_download_foreign_file_is_404(root: Path) -> None:
    """别人的产物：文件真实存在，但不在本次等待的 run 名下 → 404。"""
    row = _await_row()
    theirs = _file(root, "secret.csv")
    run = Run(id=row.run_id, agent_id=uuid4(), trigger="manual", input={})
    _use_db(_FakeDB(await_row=row, run=run, files=[theirs]))
    resp = client.get(
        f"/api/v1/open/files/{theirs.id}/content?await_id={row.id}", headers=_headers(row.id)
    )
    assert resp.status_code == 404


def test_download_missing_content_is_404(root: Path) -> None:
    """登记在案但盘上文件被清理：404 而不是 500。"""
    row = _await_row()
    f = File(
        id=uuid4(),
        path="data/files/gone.csv",
        filename="gone.csv",
        mime="text/csv",
        size=3,
        sha256="0" * 64,
    )
    run = Run(id=row.run_id, agent_id=uuid4(), trigger="manual", input={})
    _use_db(_FakeDB(await_row=row, run=run, artifacts=[_artifact(row.run_id, f.id)], files=[f]))
    resp = client.get(
        f"/api/v1/open/files/{f.id}/content?await_id={row.id}", headers=_headers(row.id)
    )
    assert resp.status_code == 404


def test_download_over_limit_is_413(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "open_file_max_bytes", 4)
    row = _await_row()
    f = _file(root, "big.csv", b"1234567890")
    run = Run(id=row.run_id, agent_id=uuid4(), trigger="manual", input={})
    _use_db(_FakeDB(await_row=row, run=run, artifacts=[_artifact(row.run_id, f.id)], files=[f]))
    resp = client.get(
        f"/api/v1/open/files/{f.id}/content?await_id={row.id}", headers=_headers(row.id)
    )
    assert resp.status_code == 413


def test_await_id_is_mandatory_for_download(root: Path) -> None:
    """不传 await_id 就没有归属依据 → 422（取件凭据与文件必须在同一个请求里）。"""
    _use_db(_FakeDB())
    resp = client.get(f"/api/v1/open/files/{uuid4()}/content", headers={"X-API-Key": TOKEN})
    assert resp.status_code == 422


# ---------- 4. 派发契约：取件地址模板 ----------


def test_pickup_url_template_carries_placeholder_and_await_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "platform_base_url", "https://platform.example.com/")
    url = pickup_url_template("abc")
    assert url == "https://platform.example.com/api/v1/open/files/{file_id}/content?await_id=abc"


def test_await_callback_carries_files_url_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = ToolContext(
        run_id="r1",
        idempotency_key="k1",
        callback_token="tk",
        await_id="a1",
        callback_url="https://p/cb",
        files_url="https://p/api/v1/open/files/{file_id}/content?await_id=a1",
    )
    payload = tools_builtin._await_callback({}, ctx)
    assert payload is not None
    assert payload["files_url"].endswith("?await_id=a1")
    # MCP 通道（_meta）同样带上，且空值不占位
    assert ctx.as_meta()["agentos"]["files_url"] == payload["files_url"]
    assert "files_url" not in ToolContext(run_id="r1").as_meta()["agentos"]

    # 加法契约：模板缺席时不出现这个键，老第三方解析器零感知
    bare = tools_builtin._await_callback(
        {},
        ToolContext(
            run_id="r1",
            idempotency_key="k1",
            callback_token="tk",
            await_id="a1",
            callback_url="https://p/cb",
        ),
    )
    assert bare is not None
    assert "files_url" not in bare


def test_await_callback_legacy_channel_keeps_files_url() -> None:
    legacy = {
        "await_id": "a1",
        "url": "https://p/cb",
        "token": "tk",
        "idempotency_key": "k1",
        "files_url": "https://p/api/v1/open/files/{file_id}/content?await_id=a1",
    }
    payload = tools_builtin._await_callback({"await_callback": legacy}, None)
    assert payload is not None
    assert payload["files_url"] == legacy["files_url"]
