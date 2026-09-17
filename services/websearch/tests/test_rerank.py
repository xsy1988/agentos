"""重排层单测：跨容器 JSON 契约、降级纪律、partial 补位、按层下发 token 上限。

这一层的纪律是**只降级不失败**：精排是收益最高的一环，但也是最贵、最容易超时的一环。
它挂掉时必须回退到 RRF 序并如实标记，而不是让整条查询失败——所以降级分支的测试
和正常分支一样多。

请求体契约（documents 条数、top_n、budget_s、max_length）也在这里钉住：
这几个字段直接决定 CPU 成本（≈ pairs × tokens²），改错了不会报错，只会变慢到超时。
"""

import asyncio
import socket
import threading
import time
from typing import Any

from websearch.config import Settings
from websearch.rerank import OK, PARTIAL, SKIPPED, doc_text, ping, rerank_docs, rerank_passages
from websearch.types import Doc


def _docs(n: int) -> list[Doc]:
    return [
        Doc(
            url=f"https://example.com/p{i}",
            norm_url=f"https://example.com/p{i}",
            title=f"标题{i}",
            snippet=f"摘要{i} 向量数据库的索引结构",
            score=float(n - i),
        )
        for i in range(n)
    ]


def _cfg(**kw: Any) -> Settings:
    base: dict[str, Any] = {"rerank_top_n": 3, "rerank_candidates": 5, "rerank_max_length": 128}
    base.update(kw)
    return Settings(_env_file=None, **base)


def _fake_cfg(fake: Any, **kw: Any) -> Settings:
    """用例需要的旋钮 + 假服务实际分到的随机端口。URL 只能从 fake.cfg 取。"""
    return _cfg(reranker_url=fake.cfg.reranker_url, **kw)


def _ok(results: list[dict[str, Any]]) -> Any:
    def responder(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, {"results": results, "scored": len(results), "partial": False}

    return responder


# ---------------------------------------------------------------- doc_text
def test_doc_text_composes_title_snippet_and_semantic_path() -> None:
    """URL 路径要语义化：`bge-reranker-v2-m3` 里的连字符对 cross-encoder 是噪声，
    拆成词之后路径本身也成了判别信号（/docs/ 与 /blog/ 不是一回事）。"""
    d = Doc(
        url="https://huggingface.co/BAAI/bge-reranker-v2-m3",
        norm_url="https://huggingface.co/BAAI/bge-reranker-v2-m3",
        title="BGE Reranker v2 m3",
        snippet="568M 参数，8192 上下文",
    )
    text = doc_text(d, 240)
    assert text.startswith("BGE Reranker v2 m3\n568M 参数，8192 上下文\n")
    assert "bge reranker v2 m3" in text


def test_doc_text_respects_limit_and_handles_empty_fields() -> None:
    d = _docs(1)[0]
    assert len(doc_text(d, 10)) <= 10
    bare = Doc(url="https://x.com/a", norm_url="https://x.com/a")
    assert doc_text(bare, 240) == "a"  # 无标题无摘要时不留空行


# ---------------------------------------------------------------- 正常路径
def test_rerank_orders_by_score_and_records_it(fake_reranker: Any) -> None:
    docs = _docs(8)
    responder = _ok(
        [{"index": 4, "score": 0.91234}, {"index": 0, "score": 0.5}, {"index": 2, "score": 0.3}]
    )
    with fake_reranker(responder) as fake:
        out, status, elapsed = asyncio.run(rerank_docs("向量数据库", docs, _fake_cfg(fake)))
    assert status == OK
    assert [d.norm_url for d in out] == [
        "https://example.com/p4",
        "https://example.com/p0",
        "https://example.com/p2",
    ]
    assert out[0].rerank_score == 0.9123  # 四位小数：够判别用，又不会让 JSON 变噪声
    assert elapsed >= 0


def test_rerank_sends_only_the_head_of_the_window(fake_reranker: Any) -> None:
    """只精排 RRF 头部：候选窗口是**召回**的度量口径（recall@50），精排只影响其中的排序。

    排在 rerank_candidates 之后的候选本来也进不了 top10，为它们花 CPU 是纯浪费——
    这条断言钉住的是成本，不是行为美观。
    """
    docs = _docs(30)
    with fake_reranker(_ok([{"index": 0, "score": 0.9}])) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=3, rerank_candidates=5)
        out, status, _ = asyncio.run(rerank_docs("向量数据库", docs, cfg))
        req = fake.last
    assert len(req["documents"]) == 5  # 30 条候选只送头部 5 条
    # top_n 要的是**全部已打分候选**（=窗口宽）而不是 rerank_top_n：低置信过滤剔掉的
    # 名额，得靠 11~20 名顶上。只截断响应，不增加打分成本
    assert req["top_n"] == 5
    assert req["query"] == "向量数据库"
    assert req["max_length"] == cfg.rerank_max_length
    assert status == PARTIAL  # 只回来 1 条 → 其余按 RRF 序补位
    assert len(out) == 3


def test_rerank_passes_soft_budget_derived_from_timeout(fake_reranker: Any) -> None:
    """budget_s = 客户端超时 - slack：让服务端**主动**在超预算时停下并返回已算部分。

    被客户端断开是一分不得，主动停下是 partial——两者对结果质量的差别很大。
    """
    with fake_reranker(_ok([{"index": 0, "score": 0.9}])) as fake:
        cfg = _fake_cfg(fake, rerank_server_slack=0.6)
        asyncio.run(rerank_docs("q", _docs(3), cfg, timeout=5.0))
        assert fake.last["budget_s"] == 4.4


def test_rerank_uses_configured_timeout_when_not_overridden(fake_reranker: Any) -> None:
    with fake_reranker(_ok([{"index": 0, "score": 0.9}])) as fake:
        cfg = _fake_cfg(fake, rerank_timeout=12.0, rerank_server_slack=0.6)
        asyncio.run(rerank_docs("q", _docs(3), cfg))
        assert fake.last["budget_s"] == 11.4


def test_partial_fills_the_rest_in_rrf_order(fake_reranker: Any) -> None:
    """预算内没打完 → 已打分的用精排序，剩余按 RRF 序补位。

    宁可混入未精排的尾部，也不要少给模型几条结果；但补位的条目**不带** rerank_score，
    这样下游（评测的 nDCG、meta 的 rerank=partial）能看出哪些是真排过的。
    """
    docs = _docs(8)
    with fake_reranker(_ok([{"index": 4, "score": 0.8}])) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=3, rerank_candidates=5)
        out, status, _ = asyncio.run(rerank_docs("q", docs, cfg))
    assert status == PARTIAL
    assert [d.norm_url for d in out] == [
        "https://example.com/p4",  # 唯一精排过的排第一
        "https://example.com/p0",  # 其余按 RRF 序补
        "https://example.com/p1",
    ]
    assert out[0].rerank_score == 0.8
    assert out[1].rerank_score is None


def test_bad_indices_from_server_are_ignored(fake_reranker: Any) -> None:
    """越界与重复索引必须被丢掉：版本不匹配的 reranker 会返回错的 index，
    直接拿去索引 window 就是 IndexError（整条查询失败），或更糟——静默排错顺序。"""
    docs = _docs(6)
    responder = _ok(
        [
            {"index": 99, "score": 0.99},
            {"index": 1, "score": 0.8},
            {"index": 1, "score": 0.7},
            {"index": -3, "score": 0.6},
            {"index": 0, "score": 0.5},
        ]
    )
    with fake_reranker(responder) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=3, rerank_candidates=5)
        out, status, _ = asyncio.run(rerank_docs("q", docs, cfg))
    assert [d.norm_url for d in out] == [
        "https://example.com/p1",
        "https://example.com/p0",
        "https://example.com/p2",
    ]
    assert status == PARTIAL
    assert out[0].rerank_score == 0.8  # 重复索引只认第一次


# ---------------------------------------------------------------- 低置信过滤
def test_low_confidence_slot_goes_to_the_next_best_candidate(fake_reranker: Any) -> None:
    """实测缺陷回归：查 "vLLM PagedAttention paper" 时两条毫不相干的页面
    （知乎 win10 报错帖、Reddit 版规）精排 0.0000 分，却因为「凑满 top10」照样交了出去。

    关键在要向服务端要回**全部**分数：只有手里握着第 4、5 名，被剔掉的第 2、3 名
    才有正常候选顶上——否则会白白少给两条结果。
    """
    responder = _ok(
        [
            {"index": 0, "score": 0.95},
            {"index": 3, "score": 0.8},
            {"index": 4, "score": 0.7},
            {"index": 2, "score": 0.0001},
            {"index": 1, "score": 0.0},
        ]
    )
    with fake_reranker(responder) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=3, rerank_candidates=5)
        out, status, _ = asyncio.run(rerank_docs("q", _docs(6), cfg))
    assert [d.norm_url for d in out] == [
        "https://example.com/p0",
        "https://example.com/p3",  # 垃圾被剔后顶上来的是它，而不是少给一条
        "https://example.com/p4",
    ]
    # 候选全打完了 → 不是 partial：标成 partial 会让编排层误报 degraded
    assert status == OK


def test_threshold_zero_disables_filtering(fake_reranker: Any) -> None:
    """阈值 0 = 关闭过滤（冷门查询宁可要噪声也不要空手时的逃生门）。"""
    responder = _ok(
        [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.0}, {"index": 2, "score": 0.0}]
    )
    with fake_reranker(responder) as fake:
        cfg = _fake_cfg(
            fake, rerank_top_n=3, rerank_candidates=5, rerank_score_threshold=0.0
        )
        out, status, _ = asyncio.run(rerank_docs("q", _docs(5), cfg))
    assert len(out) == 3
    assert status == OK


def test_all_candidates_irrelevant_returns_empty_but_stays_ok(fake_reranker: Any) -> None:
    """全部低分 → 交出 0 条，且状态仍是 ok。

    这不是降级：精排正常工作，结论就是「没一条相关」。标成 partial/unavailable 会
    让编排层报 degraded、让 Agent 以为服务坏了；而输出「未搜到相关的」是如实的。
    """
    responder = _ok([{"index": i, "score": 0.001} for i in range(5)])
    with fake_reranker(responder) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=3, rerank_candidates=5)
        out, status, _ = asyncio.run(rerank_docs("q", _docs(5), cfg))
    assert out == []
    assert status == OK


def test_filter_does_not_kill_unscored_backfill_when_partial(fake_reranker: Any) -> None:
    """预算只打完一条且那条是垃圾 → 仍按 RRF 序补位。

    未打分的候选是「不知道」，不是「不相关」：多路共识的 RRF 序本身就有信号，
    这时候交出 0 条比交出未精排的候选更差。
    """
    with fake_reranker(_ok([{"index": 0, "score": 0.0}])) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=3, rerank_candidates=5)
        out, status, _ = asyncio.run(rerank_docs("q", _docs(8), cfg))
    assert status == PARTIAL
    assert [d.norm_url for d in out] == [
        "https://example.com/p1",
        "https://example.com/p2",
        "https://example.com/p3",
    ]
    assert all(d.rerank_score is None for d in out)


def test_client_sorts_even_if_server_returns_unsorted(fake_reranker: Any) -> None:
    """不依赖「服务端已按分降序」这个跨容器约定：排错序不报错，只会静默变成乱排。"""
    responder = _ok(
        [{"index": 2, "score": 0.3}, {"index": 0, "score": 0.9}, {"index": 1, "score": 0.6}]
    )
    with fake_reranker(responder) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=3, rerank_candidates=5)
        out, _, _ = asyncio.run(rerank_docs("q", _docs(5), cfg))
    assert [d.norm_url for d in out] == [
        "https://example.com/p0",
        "https://example.com/p1",
        "https://example.com/p2",
    ]


# ---------------------------------------------------------------- 降级路径
def test_unavailable_falls_back_to_rrf_order(dead_cfg: Settings) -> None:
    """reranker 连不上 → 回退 RRF 序、标 unavailable、不抛异常。

    用缺省旋钮（top_n=10 / candidates=20）跑 8 条：全部候选都该原序交出，
    降级不是「少给几条」的借口。
    """
    docs = _docs(8)
    out, status, elapsed = asyncio.run(rerank_docs("q", docs, dead_cfg))
    assert status == "unavailable"
    assert [d.norm_url for d in out] == [f"https://example.com/p{i}" for i in range(8)]
    assert all(d.rerank_score is None for d in out)  # 没排过就不许留分数
    assert elapsed >= 0


def test_non_200_is_unavailable(fake_reranker: Any) -> None:
    with fake_reranker(lambda req: (500, {"detail": "boom"})) as fake:
        out, status, _ = asyncio.run(rerank_docs("q", _docs(4), _fake_cfg(fake)))
    assert status == "unavailable" and len(out) == 3


def test_empty_results_is_unavailable_not_zero_docs(fake_reranker: Any) -> None:
    """空结果集要当「不可用」处理：返回 0 条会让上层输出「未搜到」，
    而事实是召回成功、只是精排没给分——两件事的后续动作完全相反。"""
    with fake_reranker(lambda req: (200, {"results": []})) as fake:
        out, status, _ = asyncio.run(rerank_docs("q", _docs(4), _fake_cfg(fake)))
    assert status == "unavailable"
    assert len(out) == 3


def test_results_missing_index_field_is_unavailable(fake_reranker: Any) -> None:
    with fake_reranker(lambda req: (200, {"results": [{"score": 0.9}]})) as fake:
        _, status, _ = asyncio.run(rerank_docs("q", _docs(4), _fake_cfg(fake)))
    assert status == "unavailable"


def test_malformed_json_body_is_unavailable() -> None:
    """响应体不是合法 JSON（网关插了一段 HTML 错误页）也要走降级。

    这里用裸 socket 而不用假服务：http.server 只会发合法 JSON，
    而这种故障恰恰是「协议之外的东西」，只能自己拼一个坏响应。
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    body = b"<html>502 Bad Gateway</html>"

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.recv(65536)
            head = (
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
            )
            conn.sendall(head + body)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        cfg = _cfg(reranker_url=f"http://127.0.0.1:{port}")
        out, status, _ = asyncio.run(rerank_docs("q", _docs(3), cfg))
    finally:
        thread.join(timeout=2)
        srv.close()
    assert status == "unavailable"
    assert len(out) == 3


def test_timeout_is_unavailable(fake_reranker: Any) -> None:
    """服务端慢于客户端超时 → 降级。这条路径在生产上就是「模型在 CPU 上排队」。"""

    def slow(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        time.sleep(0.4)
        return 200, {"results": [{"index": 0, "score": 0.9}]}

    with fake_reranker(slow) as fake:
        cfg = _fake_cfg(fake, rerank_timeout=0.05)
        out, status, _ = asyncio.run(rerank_docs("q", _docs(3), cfg, timeout=0.05))
    assert status == "unavailable"
    assert len(out) == 3  # 仍然交出结果，只是按 RRF 序


def test_disabled_skips_without_calling(fake_reranker: Any) -> None:
    """RERANK_ENABLED=false 时一次 HTTP 都不该发（省下的是真金白银的 CPU 秒）。"""
    with fake_reranker(_ok([{"index": 0, "score": 0.9}])) as fake:
        cfg = _fake_cfg(fake, rerank_enabled=False)
        out, status, _ = asyncio.run(rerank_docs("q", _docs(6), cfg))
        assert fake.requests == []
    assert status == SKIPPED
    assert [d.norm_url for d in out] == [f"https://example.com/p{i}" for i in range(3)]


def test_empty_docs_skips(fake_reranker: Any) -> None:
    with fake_reranker(_ok([])) as fake:
        out, status, elapsed = asyncio.run(rerank_docs("q", [], _fake_cfg(fake)))
        assert fake.requests == []
    assert out == [] and status == SKIPPED and elapsed == 0


def test_candidates_smaller_than_top_n_is_widened(fake_reranker: Any) -> None:
    """配置写拧了（candidates < top_n）也不能让结果条数变少：窗口取两者的最大值。"""
    responder = _ok([{"index": i, "score": 0.9 - i * 0.1} for i in range(5)])
    with fake_reranker(responder) as fake:
        cfg = _fake_cfg(fake, rerank_top_n=5, rerank_candidates=2)
        out, _, _ = asyncio.run(rerank_docs("q", _docs(9), cfg))
        assert len(fake.last["documents"]) == 5
    assert len(out) == 5


# ---------------------------------------------------------------- 片段级精排
def test_passage_rerank_uses_its_own_token_budget(fake_reranker: Any) -> None:
    """文档级与片段级分别下发 max_length：片段更需要完整上下文，给的 token 预算更宽。

    CPU 上成本 ≈ pairs × tokens²，统一用最宽的那一档会让文档级白烧几倍算力。
    """
    with fake_reranker(_ok([{"index": 1, "score": 0.7}])) as fake:
        cfg = _fake_cfg(fake, rerank_max_length=128, passage_max_length=256)
        ranked = asyncio.run(rerank_passages("q", ["片段一", "片段二"], 2, cfg))
        assert fake.last["max_length"] == 256
    assert ranked == [(1, 0.7)]


def test_passage_rerank_empty_input_returns_empty_list(fake_reranker: Any) -> None:
    """[] 与 None 含义不同：前者是「没有候选」，后者是「reranker 不可用」。

    调用方按 None 退回引擎摘要，混了就会把「这页没正文」误报成「精排挂了」。
    """
    with fake_reranker(_ok([])) as fake:
        assert asyncio.run(rerank_passages("q", [], 2, _fake_cfg(fake))) == []
        assert fake.requests == []


def test_passage_rerank_unavailable_returns_none(dead_cfg: Settings) -> None:
    assert asyncio.run(rerank_passages("q", ["片段"], 2, dead_cfg)) is None


# ---------------------------------------------------------------- 探活
def test_ping_reports_unavailable_with_reason(dead_cfg: Settings) -> None:
    out = asyncio.run(ping(dead_cfg))
    assert out["available"] is False
    assert out["reason"]  # 要说清原因：search_meta 会把这行直接给模型看


def test_ping_reports_model_state(fake_reranker: Any) -> None:
    health = {"status": "ok", "model": "BAAI/bge-reranker-v2-m3", "loaded": True}
    with fake_reranker(_ok([]), health=health) as fake:
        out = asyncio.run(ping(fake.cfg))
        assert fake.last["_path"] == "/health"
    assert out["available"] is True
    assert out["loaded"] is True
    assert out["model"] == "BAAI/bge-reranker-v2-m3"
    assert isinstance(out["latency_ms"], int)


def test_ping_non_200_is_unavailable(fake_reranker: Any) -> None:
    """模型还在下载时 /health 返回 503：这时必须报不可用，
    否则流水线会把每一次精排都等到超时才降级。"""
    with fake_reranker(_ok([])) as fake:  # health=None → 503
        out = asyncio.run(ping(fake.cfg))
    assert out["available"] is False
    assert "503" in out["reason"]
