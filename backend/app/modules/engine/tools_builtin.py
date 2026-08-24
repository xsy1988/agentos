"""builtin 占位工具（M2-2c）—— DoD 验证用，M3 由 MCP/HTTP 通道接管真实工具。

capabilities 表登记（type=tool, category=builtin），payload 携带：
- builtin: 注册键（本文件 BUILTIN_TOOLS 的键）
- schema: OpenAI function-calling 格式的工具签名（agent bind_tools 直接用）

执行纪律（模块详细设计 §4 硬边界 2 的 2c 例外）：
builtin 工具在本进程内执行（无副作用演示函数）；真实外部能力仍必须走
capabilities 的 MCP/HTTP 通道，不允许图节点内嵌业务逻辑。

M4 补充：search_knowledge 是知识库的检索面（DB 查询，无副作用），与占位
工具同为进程内执行（模块详细设计 §2.4.2：知识库对引擎只是个工具）。

法规爬虫集成：外部长任务管道（爬取+LLM 提取可达几十分钟）不能同步等待
（run timeout 限制），故异步触发立即返回 + 独立的状态查询工具；产出报告
由知识库管道另行入库供 search_knowledge 检索。
"""

import asyncio
import contextlib
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

# 法规爬虫项目位置（CRAWLER_PROJECT_ROOT 可覆盖；非密钥，仅路径）
LEGAL_CRAWLER_ROOT = Path(
    os.environ.get("LEGAL_CRAWLER_PROJECT_ROOT", "")
    or "/Users/hg/工作/项目demo/法律合规部信息爬取/爬虫脚本测试"
)
LEGAL_CRAWLER_VENV_PY = LEGAL_CRAWLER_ROOT / ".venv/bin/python"
LEGAL_CRAWLER_ENV_FILE = LEGAL_CRAWLER_ROOT / "legalcrawl.env"


async def _echo(args: dict[str, Any]) -> str:
    """read 级占位：原样返回输入，验证工具链路。"""
    await asyncio.sleep(0)  # 占位异步点，保持执行器统一 await 语义
    return f"echo: {args.get('text', '')}"


async def _dangerous_demo(args: dict[str, Any]) -> str:
    """dangerous 级占位：演示高危操作确认卡片（无真实副作用）。"""
    await asyncio.sleep(0)
    return (
        f"危险操作已执行（演示，无真实副作用）：{args.get('action', 'unknown')} "
        f"target={args.get('target', '')}"
    )


async def _search_knowledge(args: dict[str, Any]) -> str:
    """知识库语义检索：pgvector 余弦 Top-K + heading_path 拼装（模块详细设计 §2.4.2）。"""
    from app.modules.knowledge.search import format_hits, search_knowledge

    query = str(args.get("query") or "").strip()
    if not query:
        return "参数错误：query 不能为空"
    folders = args.get("folders") or None
    if isinstance(folders, str):
        folders = [folders]
    k = args.get("k") or 8
    hits = await search_knowledge(query, folders, int(k))
    return format_hits(hits)


def _load_crawler_env() -> dict[str, str]:
    """读 legalcrawl.env（KEY=VALUE 行）注入子进程环境；密钥只经内存不落库。"""
    env = os.environ.copy()
    if LEGAL_CRAWLER_ENV_FILE.is_file():
        for line in LEGAL_CRAWLER_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _crawl_log_path() -> Path:
    """后台任务的 stdout/stderr 落地文件（追踪触发记录，也作运行中的标记）。"""
    return LEGAL_CRAWLER_ROOT / "爬取结果" / "agent_triggered_run.log"


async def _run_legal_crawl(args: dict[str, Any]) -> str:
    """write 级：后台异步触发法规爬虫管道，立即返回（不等待完成）。"""
    py = LEGAL_CRAWLER_VENV_PY if LEGAL_CRAWLER_VENV_PY.is_file() else None
    if py is None:
        return f"爬虫环境不可用：未找到 {LEGAL_CRAWLER_VENV_PY}"
    log = _crawl_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    # 近 30 分钟内日志有写入 → 视为仍在运行，不重复触发
    # （pipeline 自身也有 lock，这里是前置友好提示）
    import time

    if log.is_file() and time.time() - log.stat().st_mtime < 1800:
        return "爬取管道正在后台运行中，请稍后再试。可用 legal_crawl_status 查询进度。"
    cmd = [str(py), "pipeline.py"]
    skip_llm = bool(args.get("skip_llm"))
    if skip_llm:
        cmd.append("--skip-llm")
    with open(log, "ab") as f:
        subprocess.Popen(  # noqa: S603 —— 固定脚本路径，参数固定
            cmd,
            cwd=str(LEGAL_CRAWLER_ROOT),
            env=_load_crawler_env(),
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 脱离引擎进程组：后端重启不杀爬虫
        )
    return (
        f"法规爬取管道已在后台启动（{date.today()}），预计数分钟到几十分钟完成。"
        "稍后可用 legal_crawl_status 查询进度，产出报告会同步到爬虫项目目录。"
    )


async def _legal_crawl_status(args: dict[str, Any]) -> str:
    """read 级：查询爬虫最近产出概况（状态文件 + 报告目录）。"""
    if not LEGAL_CRAWLER_ROOT.is_dir():
        return f"爬虫项目目录不存在：{LEGAL_CRAWLER_ROOT}"
    out_root = LEGAL_CRAWLER_ROOT / "爬取结果"
    days = sorted(
        (d for d in out_root.iterdir() if d.is_dir() and d.name[:1].isdigit()),
        reverse=True,
    )
    if not days:
        return "尚无爬取产出。"
    take = min(int(args.get("days") or 3), 10)
    lines = [f"最近 {take} 个爬取日："]
    for d in days[:take]:
        summary = d / "crawl_summary.json"
        crawlers: dict[str, Any] = {}
        if summary.is_file():
            with contextlib.suppress(OSError, ValueError):
                crawlers = json.loads(summary.read_text(encoding="utf-8")).get("crawlers", {})
        found = sum(int(c.get("items_found") or 0) for c in crawlers.values())
        downloaded = sum(int(c.get("items_downloaded") or 0) for c in crawlers.values())
        errors = [
            n for n, c in crawlers.items() if not str(c.get("status") or "ok").startswith("ok")
        ]
        n_files = len(list(d.glob("*.pdf"))) + len(list(d.glob("*.md")))
        err_txt = f"；异常爬虫：{','.join(errors)}" if errors else ""
        lines.append(
            f"- {d.name}：发现 {found} 条 / 下载 {downloaded} 个文件；"
            f"当日产出 {n_files} 个文件{err_txt}"
        )
    # 触发日志还在写 → 运行中
    log = _crawl_log_path()
    if log.is_file():
        import time

        if time.time() - log.stat().st_mtime < 1800:
            lines.append("（注：Agent 触发的后台任务日志近期有写入，可能仍在运行）")
    return "\n".join(lines)


BUILTIN_TOOLS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "echo": _echo,
    "dangerous_demo": _dangerous_demo,
    "search_knowledge": _search_knowledge,
    "run_legal_crawl": _run_legal_crawl,
    "legal_crawl_status": _legal_crawl_status,
}


def builtin_tool_schema(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
    """构造 OpenAI function-calling 格式的工具签名（bind_tools 直接可用）。"""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }
