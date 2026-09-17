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
from datetime import date, timedelta
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


# ---- 采购报价对比 Agent 桥接（决策4：外部服务，平台只做薄编排，§5）----
# 采购 Agent 是独立外部服务（FastAPI+SQLite+异步+SSE，自带九段流水线与四类决策 REST），
# 不并入平台。平台经标准外部 Worker 集成契约 REST 桥接：触发即返回 task_id（异步范式，
# 同法规爬虫）+ 状态轮询 + 决策面拉取 + 决策回写 + 报告。服务不可用时返回友好提示、
# 不抛异常（run 不因外部服务下线而崩）。


def _procurement_base() -> str:
    """采购 Agent REST 基址（procurement_agent_base_url 可覆盖）。"""
    from app.core.config import settings

    return settings.procurement_agent_base_url.rstrip("/")


async def _procurement_request(
    method: str, path: str, *, json_body: dict[str, Any] | None = None, timeout: float = 20.0
) -> str:
    """采购 Agent REST 统一请求 + 友好降级（外部服务下线不崩 run）。"""
    import httpx

    url = f"{_procurement_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body)
            resp.raise_for_status()
            if "application/json" in resp.headers.get("content-type", ""):
                return json.dumps(resp.json(), ensure_ascii=False)
            return resp.text[:8000]
    except httpx.HTTPError as e:
        return (
            f"采购 Agent 服务不可用（{method} {url}）：{e}。"
            "请确认外部服务已启动，并检查 procurement_agent_base_url 配置。"
        )


async def _procurement_trigger(args: dict[str, Any]) -> str:
    """write 级：异步触发报价单解析，立即返回 task_id（不等完成，同法规爬虫范式）。"""
    text = str(args.get("text") or "").strip()
    report_ref = str(args.get("report_ref") or "").strip()
    if not text and not report_ref:
        return "参数错误：需提供 text（报价单文本）或 report_ref（报价单文件引用）之一。"
    return await _procurement_request(
        "POST", "/worker/trigger", json_body={"text": text, "report_ref": report_ref}, timeout=30.0
    )


async def _procurement_status(args: dict[str, Any]) -> str:
    """read 级：轮询采购解析任务状态（九段流水线进度）。"""
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return "参数错误：task_id 不能为空。"
    return await _procurement_request("GET", f"/worker/status/{task_id}")


async def _procurement_decisions(args: dict[str, Any]) -> str:
    """read 级：拉取待决策面（供应商建档/工艺确认/项目绑定/数据纠错），schema 与卡片/侧边栏对齐。"""
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return "参数错误：task_id 不能为空。"
    return await _procurement_request("GET", f"/worker/decisions/{task_id}")


async def _procurement_submit_decision(args: dict[str, Any]) -> str:
    """write 级：把侧边栏结构化回传写回采购 Agent（落库）。"""
    decision_id = str(args.get("decision_id") or "").strip()
    if not decision_id:
        return "参数错误：decision_id 不能为空。"
    return await _procurement_request(
        "POST", f"/worker/decisions/{decision_id}", json_body={"data": args.get("data")}
    )


async def _procurement_report(args: dict[str, Any]) -> str:
    """read 级：拉取对比报告/AI 综合分析（产出可经知识库管道入库供跨任务检索）。"""
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return "参数错误：task_id 不能为空。"
    return await _procurement_request("GET", f"/worker/report/{task_id}")


async def _now_datetime(args: dict[str, Any]) -> str:
    """read 级：查询当前时间（农历 + 公历 + 星期几，本地时区）。"""
    from datetime import datetime

    from app.modules.engine.lunar import format_datetime

    await asyncio.sleep(0)  # 保持执行器统一 await 语义
    return format_datetime(datetime.now())


# ---- 天气查询（Open-Meteo 公开接口，无需密钥）----

# WMO weather code → 中文描述（Open-Meteo daily.weather_code）
_WMO_WEATHER_CN: dict[int, str] = {
    0: "晴",
    1: "基本晴",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "轻毛毛雨",
    53: "毛毛雨",
    55: "浓毛毛雨",
    56: "冻毛毛雨",
    57: "浓冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "阵雪",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}


def _weather_desc(code: int) -> str:
    return _WMO_WEATHER_CN.get(code, f"未知天气（代码 {code}）")


def _parse_weather_date(s: str) -> tuple[date | None, str | None]:
    """解析 date 参数：空/今天 → 当天；明天/后天；YYYY-MM-DD / YYYYMMDD。"""
    s = s.strip()
    if not s or s == "今天":
        return date.today(), None
    if s == "明天":
        return date.today() + timedelta(days=1), None
    if s == "后天":
        return date.today() + timedelta(days=2), None
    with contextlib.suppress(ValueError):
        return date.fromisoformat(s), None
    if len(s) == 8 and s.isdigit():
        with contextlib.suppress(ValueError):
            return date(int(s[:4]), int(s[4:6]), int(s[6:8])), None
    return None, f"日期格式错误：{s}（支持 今天/明天/后天/YYYY-MM-DD）"


def _format_weather_result(g: dict[str, Any], d: date, daily: dict[str, Any]) -> str:
    """拼装单日天气结果：城市（行政区·国家） 日期：天气；气温；降水量；降水概率；最大风速。"""
    region = " · ".join(x for x in (g.get("admin1"), g.get("country")) if x)
    loc = str(g.get("name", "?")) + (f"（{region}）" if region else "")
    times = daily.get("time") or []
    if d.isoformat() not in times:
        return f"{loc} {d}：无该日期的天气数据（Open-Meteo 仅覆盖约前后 92 天）"
    i = times.index(d.isoformat())

    def v(key: str) -> Any:
        arr = daily.get(key) or []
        return arr[i] if i < len(arr) else None

    parts = [f"{loc} {d}：{_weather_desc(int(v('weather_code') or 0))}"]
    tmin, tmax = v("temperature_2m_min"), v("temperature_2m_max")
    if tmin is not None and tmax is not None:
        parts.append(f"气温 {tmin}~{tmax}℃")
    if (p := v("precipitation_sum")) is not None:
        parts.append(f"降水量 {p} mm")
    if (pp := v("precipitation_probability_max")) is not None:
        parts.append(f"降水概率 {pp}%")
    if (w := v("wind_speed_10m_max")) is not None:
        parts.append(f"最大风速 {w} km/h")
    return "；".join(parts)


async def _query_weather(args: dict[str, Any]) -> str:
    """read 级：查询指定城市与日期的天气（Open-Meteo 公开接口，无需密钥）。"""
    import httpx

    city = str(args.get("city") or "").strip()
    if not city:
        return "参数错误：city 不能为空。"
    d, err = _parse_weather_date(str(args.get("date") or ""))
    if err or d is None:
        return err or "参数错误：date 无法解析。"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            geo = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "zh", "format": "json"},
            )
            geo.raise_for_status()
            results = geo.json().get("results") or []
            if not results:
                return f"未找到城市：{city}（请检查城市名，如「北京」「上海」「Hangzhou」）"
            g = results[0]
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": g["latitude"],
                    "longitude": g["longitude"],
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,precipitation_probability_max,wind_speed_10m_max",
                    "timezone": "auto",
                    "start_date": d.isoformat(),
                    "end_date": d.isoformat(),
                },
            )
            if resp.status_code != 200:
                reason = ""
                with contextlib.suppress(Exception):
                    reason = str(resp.json().get("reason", ""))
                return f"天气查询失败（{d}）：{reason or resp.text[:200]}"
            wj = resp.json()
    except httpx.HTTPError as e:
        return f"天气服务（Open-Meteo）不可用：{e}。请稍后重试。"
    return _format_weather_result(g, d, wj.get("daily") or {})


async def _probe_url(args: dict[str, Any]) -> str:
    """read 级：依赖预检通用工具（任务架构规范「第零步依赖预检」）。

    GET/HEAD 探测任意 URL：报告可达性/状态码/延迟/响应体摘要。超时与网络
    错误不抛异常（返回结构化失败信息），run 不因探测目标下线而崩。
    """
    import time

    import httpx

    url = str(args.get("url") or "").strip()
    if not url:
        return "参数错误：url 不能为空。"
    method = str(args.get("method") or "GET").strip().upper()
    if method not in ("GET", "HEAD"):
        method = "GET"
    timeout = min(float(args.get("timeout") or 10.0), 30.0)
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.request(method, url)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            body_snippet = ""
            if method == "GET" and "application/json" in resp.headers.get("content-type", ""):
                body_snippet = resp.text[:500]
            return json.dumps(
                {
                    "reachable": True,
                    "url": url,
                    "status_code": resp.status_code,
                    "elapsed_ms": elapsed_ms,
                    "content_type": resp.headers.get("content-type", ""),
                    "body_snippet": body_snippet,
                },
                ensure_ascii=False,
            )
    except httpx.HTTPError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return json.dumps(
            {
                "reachable": False,
                "url": url,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": elapsed_ms,
                "hint": "目标服务未启动、地址错误或网络不可达；请确认依赖服务状态。",
            },
            ensure_ascii=False,
        )


BUILTIN_TOOLS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "echo": _echo,
    "dangerous_demo": _dangerous_demo,
    "search_knowledge": _search_knowledge,
    "run_legal_crawl": _run_legal_crawl,
    "legal_crawl_status": _legal_crawl_status,
    "now_datetime": _now_datetime,
    "query_weather": _query_weather,
    "probe_url": _probe_url,
    "procurement_trigger": _procurement_trigger,
    "procurement_status": _procurement_status,
    "procurement_decisions": _procurement_decisions,
    "procurement_submit_decision": _procurement_submit_decision,
    "procurement_report": _procurement_report,
}


def builtin_tool_schema(name: str, description: str, params: dict[str, Any]) -> dict[str, Any]:
    """构造 OpenAI function-calling 格式的工具签名（bind_tools 直接可用）。"""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }
