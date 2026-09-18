"""外部服务取件通道（方案 §5 P2-1）：边界优先的只读取件。

设计取舍（为什么不做"按 file_id 取"的通路）：

- **授权靠归属，不靠角色**：`/open` 只有一把平台级 `X-API-Key`，它证明"你是平台认可的
  集成方"，**不**证明"这份文件归你"。所以本通道再叠一层归属判定——调用方必须出示
  **本次等待的 `callback_token`**（HMAC 派生、按 await 隔离），文件必须落在**该等待所属
  run** 的产出/输入集合内。第三方拿自己那笔等待的凭据只能取到自己那个 run 的文件。
- **无列举入口**：不提供"按会话/按目录/按 owner 列表"这类可遍历的入口，列表也只按
  单个 `await_id` 反查（`GET /open/awaits/{id}/files`）——遍历面收敛到"已知一笔等待"。
- **只读**：仅 `GET`，不提供上传/删除/移动，取件通道不是文件管理通道。
- **不区分 404/403**：`callback_token` 不对 → 403（凭据错，可以改）；文件不存在或
  不归这个 run → 一律 404（不给出"这份文件存在但你没权限"的存在性预言）。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.files.models import File
from app.modules.runs.models import Run, RunArtifact


async def await_run_id(db: AsyncSession, await_id: UUID) -> UUID | None:
    """等待行 → run_id（取件范围的唯一入口）。"""
    from app.modules.awaits.models import AwaitBroker

    row = await db.get(AwaitBroker, await_id)
    return row.run_id if row is not None else None


def _attachment_ids(run: Run) -> list[str]:
    """run.input.attachment_ids（派发时平台随消息收下的输入附件）。"""
    raw = (run.input or {}).get("attachment_ids")
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


async def list_run_files(db: AsyncSession, run_id: UUID) -> list[dict]:
    """本次等待所属 run 可取的文件清单：产出（`run_artifacts.file_id`）+ 输入附件。

    两条来源都是**平台自己登记过的事实**，不是目录扫描——`files` 表里有但不属于该 run
    的文件不出现在这里（也无法用任何参数把它拉进来）。
    """
    out: list[dict] = []
    seen: set[str] = set()

    artifacts = await db.scalars(
        select(RunArtifact).where(
            RunArtifact.run_id == run_id,
            RunArtifact.file_id.is_not(None),  # noqa: E711
        )
    )
    for a in artifacts:
        fid = str(a.file_id)
        if fid in seen:
            continue
        seen.add(fid)
        out.append(
            {
                "file_id": fid,
                "name": a.name or "产物",
                "mime": a.mime or "application/octet-stream",
                "size": int(a.size or 0),
                "source": "artifact",
            }
        )

    run = await db.get(Run, run_id)
    ids = _attachment_ids(run) if run is not None else []
    if ids:
        rows = await db.scalars(select(File).where(File.id.in_([UUID(x) for x in ids])))
        for f in rows:
            fid = str(f.id)
            if fid in seen:
                continue
            seen.add(fid)
            out.append(
                {
                    "file_id": fid,
                    "name": f.filename,
                    "mime": f.mime or "application/octet-stream",
                    "size": int(f.size or 0),
                    "source": "input",
                }
            )
    return out


async def get_run_file(db: AsyncSession, run_id: UUID, file_id: UUID) -> File | None:
    """归属校验 + 取实体：不属于该 run 的文件返回 None（调用方统一转 404）。"""
    allowed = {item["file_id"] for item in await list_run_files(db, run_id)}
    if str(file_id) not in allowed:
        return None
    return await db.get(File, file_id)
