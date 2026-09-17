"""服务配置（全部 env 驱动）。

容器内用 compose service name 互访（http://searxng:8080 …）；宿主直跑用 localhost 映射端口。
调参入口集中在本文件——每个字段对应「调它治什么病」，见 docs/自建网页搜索服务.md 调优表。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env",), extra="ignore")

    # ---------- 上游组件 ----------
    searxng_url: str = "http://localhost:8201"
    reranker_url: str = "http://localhost:8202"
    crawler_url: str = "http://localhost:8203"
    # Crawl4AI ≥0.9 的 socket 守卫：未设 token 只绑容器内 loopback（宿主/同网容器都连不上），
    # 因此 compose 必须设 CRAWL4AI_API_TOKEN，并把它同样注入本服务
    crawler_token: str = ""
    # 服务自身监听（MCP /mcp 与 REST 同端口同进程）
    host: str = "0.0.0.0"  # noqa: S104 —— 容器内监听，宿主端口只绑 127.0.0.1
    port: int = 8200

    # ---------- 召回（recall）----------
    # 单路超时 / 召回总预算：调小 → 更快但可能少一路；调大 → 慢引擎拖累整体
    recall_route_timeout: float = 8.0
    recall_total_budget: float = 10.0
    recall_pages: int = 1  # 每路取几页（SearXNG pageno=1..n）；加大提升 recall@50 但更慢
    # 覆盖 SearXNG 引擎清单（逗号分隔）；空 = 用 searxng/settings.yml 的默认集。
    # 字段名带 searxng 前缀：`ENGINES` 这种通用名在宿主 .env 里太容易撞上别的东西
    searxng_engines: str = ""
    # 单路结果上限（SearXNG 一页通常 10~20 条）
    recall_per_route_limit: int = 30

    # ---------- 融合（fuse）----------
    rrf_k: int = 60  # RRF 平滑常数：调小 → 头部更强势；调大 → 各路更平均
    # 路权重：original/phrase/keyword/extra（extra = 英文实体路或时效路）
    route_weights: str = "original:1.0,phrase:0.9,keyword:0.8,extra:0.7"
    candidate_window: int = 50  # 送入重排的候选窗口：召回上限就在这里封顶

    # ---------- 重排（rerank）----------
    # CPU 成本模型（arm64 / 10 核 / batch=8 实测）：128 token ≈ 0.18s/对、251 token ≈ 0.53s/对。
    # 成本 ≈ pairs × tokens²，所以两个旋钮都要拧：只排头部 + 压 token 上限。
    # 50 对 × 512 token = 44s（撞硬超时）→ 20 对 × 128 token ≈ 3.6s，而 top10 结果不变：
    # 排在 RRF 20 名之后的候选本来就进不了 top10，为它们花 40 秒是纯浪费。
    rerank_enabled: bool = True
    rerank_timeout: float = 15.0
    rerank_top_n: int = 10  # 精排后保留条数
    rerank_candidates: int = 20  # 只精排 RRF 头部 N 条；调大 → 更稳但线性变慢
    rerank_max_length: int = 128  # 文档级 token 上限：标题+摘要的判别信息集中在前段
    # 送进 cross-encoder 的文档文本上限（字符）：与 max_length 对齐，多给的部分只会被截掉
    rerank_doc_chars: int = 240
    # 文档级低置信过滤：cross-encoder 分数低于此值的候选直接丢弃（0 = 关闭）。
    # 实测动机：查 "vLLM PagedAttention paper" 时，某一路召回了两条毫不相干的页面
    # （知乎 win10 报错帖、Reddit 版规），精排给了 0.0000 分，却因为「凑满 top10」
    # 照样交了出去——对 Agent 来说这不是中性噪声，是会被当真引用的假证据。
    # 0.05 只切掉「模型明确判为无关」的那一档（实测相关页 0.74~0.98，垃圾页 0.0000）；
    # 调到 0.2~0.3 会更干净，但冷门查询可能一条不剩
    rerank_score_threshold: float = 0.05

    # ---------- 抽取（extract）----------
    # auto = trafilatura 快路，正文不足或被拦时升级 Crawl4AI；也可强制 crawl4ai/trafilatura/off
    extractor: str = "auto"
    # 对精排 top10 的前 N 篇抽正文：这是延迟主要来源（抓取 + 片段级精排都按篇数线性增长）。
    # 3 篇已能覆盖模型实际会引用的证据量；调到 5 会让全链路多花 4~6s
    extract_top_n: int = 3
    extract_concurrency: int = 5
    extract_page_timeout: float = 10.0
    extract_total_budget: float = 12.0
    extract_min_chars: int = 400  # 快路正文少于此值 → 升级 Crawl4AI
    passages_per_doc: int = 2  # 每篇最终交出的片段数
    passage_score_threshold: float = 0.30  # 片段级精排阈值（sigmoid 分数），不过则退回引擎摘要
    passage_rerank_timeout: float = 8.0
    # 片段级二次精排的候选数：一页正文能切出几十块，全送 cross-encoder 在 CPU 上是几十秒。
    # 先用零成本的词覆盖分预筛到这几块，再让 cross-encoder 在其中定胜负（与主漏斗同构）
    passage_candidates: int = 4
    passage_max_length: int = 256  # 片段比文档更需要完整上下文，给的 token 预算也更宽
    # 片段展示长度上限（字符）
    passage_chars: int = 400

    # ---------- 切片（chunk）----------
    # 取 400–600 区间的下端：400 字符 ≈ 250 token，正好落进 passage_max_length 不被截断；
    # 块更大则尾部内容会被 token 上限切掉，等于花了钱却没让模型看到
    chunk_size: int = 400  # 目标块大小（字符）；调小 → 片段更聚焦但上下文更碎
    chunk_overlap: int = 60

    # ---------- 输出预算 ----------
    # 平台不截断工具结果，且上下文压缩阈值 24000 字符 → 预算必须由服务端强制
    max_chars_default: int = 6000
    max_chars_hard_cap: int = 12000
    # 下限：再小就装不下「一条结果 + meta 行」，输出会变成 0 条，
    # 而「预算不够所以只给 0 条」与「上游没搜到」必须让模型分得清
    max_chars_floor: int = 800
    max_results_default: int = 8
    max_results_hard_cap: int = 10

    # ---------- 全链路硬超时 ----------
    # 必须 < 平台冒烟单用例 30s（backend capabilities/smoke.py），且远小于探活熔断窗口 180s
    pipeline_hard_timeout: float = 25.0
    # 阶段预算份额：各阶段的配置超时是**上限**，实际可用预算还要看硬超时的剩余时间。
    # 实测：召回 5s + 抽取 10s 就已 15s，若再按上限叠精排 15s 必然撞硬超时——
    # 所以每阶段只拿「剩余时间 × 份额」，保证总是能在 25s 内交出部分结果而不是空手。
    recall_budget_share: float = 0.45
    rerank_budget_share: float = 0.45
    extract_budget_share: float = 0.90
    min_stage_budget: float = 1.0
    # 传给 reranker 容器的软预算（= 客户端超时 - 此值）：让它提前停止打分并返回已算部分，
    # 而不是被客户端断开后一无所有（partial 比 fallback 到 RRF 序好得多）
    rerank_server_slack: float = 0.6

    # ---------- 缓存 ----------
    cache_dir: Path = Path("data")
    ttl_query_default: int = 3600  # freshness=any：1 小时
    ttl_query_fresh: int = 900  # 时效意图（day/week）：15 分钟
    ttl_query_evergreen: int = 604800  # 技术常青类：7 天
    ttl_url_default: int = 604800  # 正文缓存 7 天
    ttl_url_fresh: int = 86400  # 时效类正文 24 小时
    # 延迟样本保留条数（search_meta 报分位用）
    latency_samples: int = 200

    # ---------- 查询理解 ----------
    # LLM 检索式扩写：默认关（零按次成本 + 评测结果可复现）
    route_llm: bool = False
    llm_gateway_base_url: str = "http://localhost:18080/v1"
    llm_gateway_api_key: str = "local-demo-key"
    llm_route_model: str = ""
    llm_route_timeout: float = 6.0

    # ---------- 派生 ----------
    @property
    def weight_map(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for part in self.route_weights.split(","):
            name, _, val = part.strip().partition(":")
            if name:
                try:
                    out[name] = float(val)
                except ValueError:
                    continue
        return out or {"original": 1.0}

    @property
    def engine_list(self) -> list[str]:
        return [e.strip() for e in self.searxng_engines.split(",") if e.strip()]

    @property
    def cache_path(self) -> Path:
        return self.cache_dir / "websearch.db"


settings = Settings()
