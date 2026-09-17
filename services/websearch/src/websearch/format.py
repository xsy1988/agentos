"""输出层：MCP 文本（LLM 直接消费）+ REST JSON（评测与调试）。

为什么预算必须在服务端强制：平台引擎不截断工具结果（graph 里直接 `ToolMessage(str(content))`），
而上下文压缩阈值是 24000 字符——一次搜索吐出三万字，等于把整段会话历史挤掉。
所以 `max_chars` 默认 6000、硬上限 12000，超预算按序截断并在 meta 里如实标 `truncated`。

文本格式刻意做成「编号 + 元信息一行 + ▸ 片段」：编号让模型能在回答里引用来源，
元信息一行给足 url/站点/日期，▸ 只放片段级精排选出的证据段。
"""

from typing import Any

from websearch.config import Settings
from websearch.types import SearchResult

_PASSAGE_MARK = "▸"

# 「召回被掐断」的判据是**效果侧**的：真正交出候选的引擎数 ≤ 这个值时，
# 就不能再对模型说「未搜到」。
# 实测动机：一轮评测里 brave/duckduckgo/google/google cse/mojeek 被 SearXNG 集体挂起
# （Suspended: too many requests），只剩 bing 一路在出**无关**结果（查 PostgreSQL VACUUM
# 返回 cn.ubuntu.com），精排如实判为全不相关 → 返回 0 条。此时「未搜到与 X 相关的结果」
# 是一句**关于互联网的错误陈述**：模型会据此回答「网上没有这方面的资料」，而真相是我们的
# 召回被掐断了。
# 定 2 而不是 0：单个引擎独木支撑时结论同样不可信；而 6~7 路都在出数、精排仍判全不相关，
# 那才是可以如实说「没搜到」的情形。
# 为什么不用「几个引擎无响应」当判据：那个字段只说谁报错了，而实测最坑的引擎不报错
# （bing 稳定返回 10 条无关结果，永远不会出现在 unresponsive_engines 里）。
_THIN_RECALL_ENGINES = 2


def clamp_max_chars(value: int, cfg: Settings) -> int:
    """输出预算钳制：≤0 用默认值，上限是硬顶，下限保证至少装得下「一条结果 + meta 行」。"""
    if value <= 0:
        return cfg.max_chars_default
    return max(cfg.max_chars_floor, min(value, cfg.max_chars_hard_cap))


def clamp_max_results(value: int, cfg: Settings) -> int:
    if value <= 0:
        return cfg.max_results_default
    return min(value, cfg.max_results_hard_cap)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _doc_block(idx: int, d: Any, passage_chars: int) -> str:
    """单条结果的文本块。"""
    meta_bits = [d.norm_url or d.url]
    if d.site:
        meta_bits.append(d.site)
    if d.date:
        meta_bits.append(d.date)
    lines = [f"{idx}. {d.title or '(无标题)'}", "   " + " | ".join(meta_bits)]
    passages = d.passages or ([d.snippet] if d.snippet else [])
    for p in passages[:3]:
        text = " ".join(str(p).split())
        if not text:
            continue
        if len(text) > passage_chars:
            text = text[:passage_chars].rstrip() + "…"
        lines.append(f"   {_PASSAGE_MARK} {text}")
    return "\n".join(lines)


def meta_line(result: SearchResult, *, truncated: int = 0, dropped: int = 0) -> str:
    """一行流水线自报（可观测性优先：每层做了什么、降级了没有，一眼看清）。"""
    stats = result.stats or {}
    flags = result.flags or {}
    extract_flag = flags.get("extract", "-")
    if "/" in extract_flag:
        extract_txt = extract_flag
    else:
        done = stats.get("extracted", 0)
        tried = stats.get("extract_attempted", 0)
        extract_txt = f"{done}/{tried}:{extract_flag}"
    bits = [
        f"routes={stats.get('routes', len(result.route_names))}",
        f"recall={stats.get('raw', 0)}",
        f"dedup={stats.get('after_dedup', 0)}",
        f"window={stats.get('window', len(result.window))}",
        f"agreement={stats.get('route_agreement', 0)}",
        f"rerank={flags.get('rerank', '-')}",
        f"extract={extract_txt}",
        f"cache={flags.get('cache', 'miss')}",
        f"elapsed={result.elapsed_ms / 1000:.1f}s",
        f"chars={result.output_chars}",
    ]
    if result.unresponsive_engines:
        bits.append("unresponsive=" + ",".join(result.unresponsive_engines[:5]))
    if dropped:
        bits.append(f"dropped={dropped}")
    if truncated:
        bits.append("truncated=yes")
    if flags.get("degraded"):
        bits.append(f"degraded={flags['degraded']}")
    if result.errors:
        bits.append(f"errors={len(result.errors)}")
    return "meta: " + " ".join(bits)


def format_text(result: SearchResult, cfg: Settings) -> str:
    """MCP 工具返回的文本。预算截断按序进行（保排序，不保数量）。"""
    if not result.available:
        return (
            f"网页搜索暂不可用：{result.message or '上游 SearXNG 未响应'}\n"
            "（这是服务端如实降级，不是查询无结果；可稍后重试或换更具体的关键词）"
        )
    if not result.docs:
        down = result.unresponsive_engines
        contrib = [str(e) for e in ((result.stats or {}).get("contributing_engines") or [])]
        if len(contrib) <= _THIN_RECALL_ENGINES:
            # 先说降级、再说结果：顺序就是优先级，模型往往只读第一句
            head = (
                f"网页搜索降级：只有 {len(contrib)} 个引擎给出了结果（{','.join(contrib) or '无'}）"
            )
            if down:
                head += f"，{len(down)} 个搜索引擎无响应（{','.join(down[:6])}）"
            head += (
                "——召回被掐断了。\n"
                "这**不等于**「网上没有这方面的资料」：请换个说法重试，或稍后再试"
                "（引擎挂起有退避期，通常 1~10 分钟自恢复）。"
            )
        else:
            head = f"未搜到与「{result.params.query}」相关的结果。"
        if result.errors:
            head += f"\nreason: {result.errors[0][:160]}"
        if down:
            head += f"\nunresponsive_engines: {','.join(down)}"
        return head + "\n" + meta_line(result)

    budget = clamp_max_chars(result.params.max_chars, cfg)
    passage_chars = max(120, cfg.passage_chars)
    # 预留量按**实际的** head 与 meta 长度算，而不是拍一个常数：
    # meta 带上 unresponsive/degraded/errors 时会明显变长，常数预留会让总长突破 max_chars。
    # 而「不突破预算」是对平台的硬承诺：引擎不截断工具结果，多出来的字符会直接挤掉会话历史
    head_worst = f"网页搜索：{_clip(result.params.query, 120)}（{len(result.docs)} 条）"
    probe_meta = meta_line(result, truncated=1, dropped=len(result.docs))
    reserve = len(head_worst) + len(probe_meta) + 16  # 16 的余量吸收 chars= 位数变化
    room_budget = max(0, budget - reserve)

    blocks: list[str] = []
    used = 0
    truncated = 0
    for i, d in enumerate(result.docs, start=1):
        block = _doc_block(i, d, passage_chars)
        room = room_budget - used - 2
        if len(block) > room:
            if not blocks:
                # 第一条无论如何都要给：「0 条」的输出与「没搜到」在模型看来没有区别，
                # 而这两件事必须分得清——前者是预算不够，后者是上游确实没有结果
                keep = max(40, room - 120)  # 120 ≈ 标题行 + 元信息行的开销
                block = _doc_block(i, d, keep)[: max(60, room)]
                blocks.append(block)
                used += len(block) + 2
            truncated = 1
            break
        blocks.append(block)
        used += len(block) + 2
    dropped = len(result.docs) - len(blocks)

    head = f"网页搜索：{_clip(result.params.query, 120)}（{len(blocks)} 条）"
    body = head + "\n\n" + "\n\n".join(blocks)
    # meta 行要自报 chars=，而它自己也算在 chars 里 → 迭代到不动点。
    # 固定跑两遍不够：位数进位（chars=999 → 1000）会让长度再变 1，
    # 而对外自报的长度与实际不符就是说谎（评测拿它算输出预算）
    out = f"{body}\n\n{meta_line(result, truncated=truncated, dropped=dropped)}"
    for _ in range(4):
        result.output_chars = len(out)
        rebuilt = f"{body}\n\n{meta_line(result, truncated=truncated, dropped=dropped)}"
        if rebuilt == out:
            break
        out = rebuilt
    result.output_chars = len(out)
    return out


def format_fetch(data: dict[str, Any]) -> str:
    """`web_fetch` 的文本输出。"""
    if not data.get("available"):
        return f"抓取失败：{data.get('message', '未知原因')}（url={data.get('url')}）"
    head = f"网页正文：{data.get('url')}"
    bits = [
        f"site={data.get('site') or '-'}",
        f"source={data.get('source')}",
        f"chars={data.get('chars_total')}",
        f"chunks={data.get('chunks')}",
        f"elapsed={int(data.get('elapsed_ms', 0)) / 1000:.1f}s",
    ]
    if data.get("truncated"):
        bits.append("truncated=yes")
    passages = data.get("passages") or []
    lines = [head, "meta: " + " ".join(bits), ""]
    if passages:
        lines.append("与问题最相关的片段：")
        lines.extend(f"{_PASSAGE_MARK} {' '.join(str(p).split())}" for p in passages)
        lines.append("")
    lines.append("正文：")
    lines.append(str(data.get("text") or ""))
    return "\n".join(lines)


def to_json(result: SearchResult, cfg: Settings) -> dict[str, Any]:
    """REST `/search` 的结构化输出：同等信息 + 各阶段耗时 + 各路排名 + 精排分数。"""
    data = result.to_dict()
    data["stage_enabled"] = list(result.params.stages)
    data["max_chars"] = clamp_max_chars(result.params.max_chars, cfg)
    return data
