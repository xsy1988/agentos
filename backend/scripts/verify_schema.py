"""只读校验：真实库的 v1.6 关键对象是否与 ORM 声明逐字一致。

与 `alembic check` 的分工：
- `alembic check` 回答"元数据与库之间还有没有待生成的 upgrade"（全量、但输出是 diff）；
- 本脚本对 v1.6 的承重对象做定点断言，把期望 DDL **由 ORM 声明编译得到**，
  与人写的字面量解耦——ORM 改了而库没跟上（或反过来）会立刻失败。
  这正是本轮修掉的 4 处漂移（LangGraph 表过滤、HNSW 索引、memory_files 约束名与 date 空值性）
  最容易溜过去的地方。

用法（backend/ 下，只读、可反复跑）：
    uv run python -m scripts.verify_schema
    uv run python -m scripts.verify_schema "postgresql+asyncpg://user:pw@host:5432/db"
"""

import asyncio
import re
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateIndex

from app.core.config import settings
from app.db import models  # noqa: F401  导入全部模型，确保 metadata 完整
from app.db.base import Base

# 期望"库中存在"的索引，及其所属表（索引名 -> 表名）
INDEX_CHECKS: dict[str, str] = {
    # P0-1 时长账本：巡检按 deadline_at 扫超时 run
    "ix_runs_deadline_at": "runs",
    # P1-9 幂等键：表达式索引 + 部分条件
    "uq_runs_client_message_id": "runs",
    # M4a 向量检索：HNSW 索引（autogenerate 看不见就会误删）
    "ix_kb_chunks_embedding_hnsw": "kb_chunks",
}

# 唯一约束必须逐名对账（DB 里的名字与命名约定可能不一致）
UNIQUE_CONSTRAINT_CHECKS: list[tuple[str, str, tuple[str, ...]]] = [
    ("memory_files", "uq_memory_files_kind_date", ("kind", "date")),
]

# v1.6 新增表
TABLE_CHECKS: tuple[str, ...] = ("run_artifacts", "await_broker")

# v1.6 新增列：(表, 列, information_schema 的 data_type)
COLUMN_CHECKS: list[tuple[str, str, str]] = [
    ("runs", "active_ms", "integer"),
    ("runs", "deadline_at", "timestamp with time zone"),
]

# 空值性对账：(表, 列, 是否允许 NULL)
NULLABLE_CHECKS: list[tuple[str, str, bool]] = [
    # platform 记忆的 date 为 NULL 是合法数据；ORM 若被误判成 NOT NULL，
    # autogenerate 会生成 SET NOT NULL 直接打断写入。
    ("memory_files", "date", True),
]


def normalize(ddl: str) -> str:
    """把 PG 打印的 indexdef 与 SQLAlchemy 编译结果拉到同一形态再比对。

    抹平的是"方言表述差异"，不是结构差异：schema 限定名、`::text` 显式转换、
    btree 的冗余 `USING btree`、表达式索引多套的一层括号。索引名/表名/唯一性/
    访问方法/列与表达式顺序/部分索引条件都保留。
    """
    s = ddl.strip().rstrip(";")
    s = re.sub(r"::text\b", "", s)
    s = re.sub(r"\bpublic\.", "", s)
    s = re.sub(r"\s+USING btree\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[()]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def orm_index_ddl(name: str) -> str | None:
    """按索引名在 ORM metadata 里找到声明并编译成 PG DDL。"""
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            if index.name == name:
                return str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    return None


def expected_head() -> str:
    """迁移链的单头；多头说明被 rebase 坏了，本身就是错误。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"迁移链有 {len(heads)} 个头：{heads}")
    return heads[0]


async def verify(dsn: str) -> list[str]:
    """返回失败项描述；空列表 = 全绿。"""
    failures: list[str] = []
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:

            async def scalar(sql: str, **params: object) -> object:
                return (await conn.execute(text(sql), params)).scalar()

            print("== v1.6 schema 对账 ==")

            # 1) 迁移版本落在单头
            head = expected_head()
            current = await scalar("SELECT version_num FROM alembic_version")
            print(
                f"[{'OK' if current == head else 'FAIL'}] alembic_version = {current}（head={head}）"
            )
            if current != head:
                failures.append(f"alembic_version={current} 未在 head={head}")

            # 2) 表存在
            for table in TABLE_CHECKS:
                exists = await scalar("SELECT to_regclass(:q) IS NOT NULL", q=f"public.{table}")
                print(f"[{'OK' if exists else 'FAIL'}] 表 {table}")
                if not exists:
                    failures.append(f"缺表 {table}")

            # 3) 列与类型
            for table, column, data_type in COLUMN_CHECKS:
                actual = await scalar(
                    "SELECT data_type FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = :c",
                    t=table,
                    c=column,
                )
                ok = actual == data_type
                print(
                    f"[{'OK' if ok else 'FAIL'}] 列 {table}.{column} = {actual}（期望 {data_type}）"
                )
                if not ok:
                    failures.append(f"{table}.{column} 类型为 {actual}，期望 {data_type}")

            # 4) 空值性
            for table, column, nullable in NULLABLE_CHECKS:
                actual = await scalar(
                    "SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = :c",
                    t=table,
                    c=column,
                )
                ok = (actual == "YES") is nullable
                print(
                    f"[{'OK' if ok else 'FAIL'}] {table}.{column} nullable={actual}"
                    f"（期望 {'YES' if nullable else 'NO'}）"
                )
                if not ok:
                    failures.append(f"{table}.{column} 空值性与期望不符：{actual}")

            # 5) 索引：DB 的 indexdef 必须等于 ORM 编译出来的 DDL
            for name, table in INDEX_CHECKS.items():
                actual = await scalar(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'"
                    " AND tablename = :t AND indexname = :n",
                    t=table,
                    n=name,
                )
                if actual is None:
                    print(f"[FAIL] 索引 {name}（表 {table}）不存在")
                    failures.append(f"缺索引 {name}")
                    continue
                expected = orm_index_ddl(name)
                if expected is None:
                    print(f"[FAIL] 索引 {name} 在 ORM metadata 中找不到声明")
                    failures.append(f"ORM 缺少索引声明 {name}")
                    continue
                ok = normalize(str(actual)) == normalize(expected)
                print(f"[{'OK' if ok else 'FAIL'}] 索引 {name}")
                if not ok:
                    print(f"      库: {normalize(str(actual))}")
                    print(f"      ORM: {normalize(expected)}")
                    failures.append(f"索引 {name} 定义与 ORM 不一致")

            # 6) 唯一约束：名字与列序都要对
            for table, name, columns in UNIQUE_CONSTRAINT_CHECKS:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT a.attname FROM pg_constraint c"
                            " JOIN pg_class t ON t.oid = c.conrelid"
                            " JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE"
                            " JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum"
                            " WHERE c.conname = :n AND t.relname = :t ORDER BY k.ord"
                        ),
                        {"n": name, "t": table},
                    )
                ).scalars()
                actual_cols = tuple(rows)
                ok = actual_cols == columns
                print(f"[{'OK' if ok else 'FAIL'}] 唯一约束 {table}.{name} = {actual_cols}")
                if not ok:
                    failures.append(f"唯一约束 {name} 列为 {actual_cols}，期望 {columns}")
    finally:
        await engine.dispose()
    return failures


async def main() -> None:
    dsn = sys.argv[1] if len(sys.argv) > 1 else settings.database_url
    safe = dsn.split("@")[-1]
    print(f"目标库：{safe}")
    failures = await verify(dsn)
    print()
    if failures:
        print(f"❌ {len(failures)} 项不通过：")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)
    print("✅ 全部通过：库与 ORM 声明一致")


if __name__ == "__main__":
    asyncio.run(main())
