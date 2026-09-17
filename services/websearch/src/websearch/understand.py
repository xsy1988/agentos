"""查询理解：一次意图 → 最多 4 路互补检索式（确定性规则，零成本）。

多路互补的收益来自「不同引擎/不同检索式的工作方式差异」：
- original：整句交给引擎，吃它们的语义与改写能力
- phrase：显著短语加引号，压住「关键词都命中但在讲另一件事」的噪声
- keyword：去虚词后的关键词串，对中文分词粒度差异免疫（jieba 切一遍）
- extra：英文实体路（中英混查时捞英文原始资料）或时效路（time_range 收窄）

LLM 扩写默认关闭（ROUTE_LLM=false）：保持零按次成本，且同一 query 多次跑结果稳定可评测。
"""

import logging
import re
from dataclasses import dataclass

from websearch.config import Settings

logger = logging.getLogger(__name__)

# 中文虚词/疑问词（关键词路要丢掉的东西）
_ZH_STOPWORDS = {
    "的",
    "了",
    "和",
    "与",
    "及",
    "或",
    "在",
    "是",
    "有",
    "个",
    "中",
    "为",
    "被",
    "把",
    "对",
    "从",
    "到",
    "也",
    "都",
    "就",
    "而",
    "但",
    "如果",
    "因为",
    "所以",
    "什么",
    "怎么",
    "怎样",
    "如何",
    "为什么",
    "哪些",
    "哪个",
    "多少",
    "是否",
    "可以",
    "需要",
    "请问",
    "帮我",
    "一下",
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "以及",
    "关于",
    "通过",
    "进行",
    "相关",
    "介绍",
}
_EN_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "for",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "be",
    "with",
    "what",
    "how",
    "why",
    "which",
    "do",
    "does",
    "did",
    "can",
    "i",
    "you",
    "we",
    "me",
    "my",
    "please",
}

# 时效信号 → SearXNG time_range
_RECENCY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("day", re.compile(r"今天|今日|当天|刚刚|最新一?天|past day", re.I)),
    ("week", re.compile(r"本周|这周|最近一周|近一周|最近几天|这几天|past week", re.I)),
    (
        "month",
        re.compile(r"最新|最近|近期|本月|这个月|今年|近来|新发布|刚发布|更新|进展|动态|新闻"),
    ),
)

# 常青信号（技术文档类，可长缓存）
# 「什么是 X」与「X 是什么」都要收：定义型查询是常青的最典型形态（定义不随时间变），
# 少收一种写法就等于让同一类查询在缓存 TTL 上分成两档
_EVERGREEN_RE = re.compile(
    r"文档|手册|教程|规范|标准|原理|参数|api|sdk|github|RFC|语法|定义|是什么|什么是|什么叫|何谓|含义|规格",
    re.I,
)

# 技术信号 → 追加 SearXNG 的 `it` 类目。实测：categories=it 会把 github / stackoverflow /
# hackernews / mdn / docker hub 拉进召回：同一句「transformer attention is all you need」
# general 只 31 条、it 有 85 条 —— 这是免费的召回上限抬升
_TECH_RE = re.compile(
    r"api|sdk|github|repo|代码|函数|数据库|代码库|依赖库|框架|参数|配置|版本|安装|部署|报错|异常|调试|日志|"
    r"docker|kubernetes|k8s|python|java|golang|rust|typescript|sql|模型|推理|微调|"
    r"error|exception|install|config|version|release|changelog|benchmark|tutorial", re.I
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 拉丁/数字实体（产品名、版本号、模型名、代码符号）
_LATIN_ENTITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+\-]{2,}")
# 连续中文串（按非中文字符切）
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{4,}")


@dataclass
class Route:
    """一路检索式。weight 是 RRF 融合的票权，reason 让「为什么有这一路」可观测。"""

    name: str
    query: str
    weight: float = 1.0
    time_range: str = ""  # ""|day|week|month|year
    categories: str = "general"
    reason: str = ""


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _bare(text: str) -> str:
    """去掉首尾引号并小写，用于判断两路检索式是否**实质同构**。"""
    return _clean(text).strip("\"“”'").lower()


def detect_time_range(query: str, freshness: str) -> str:
    """时效意图 → SearXNG time_range。显式 freshness 优先于自动检测。"""
    if freshness in ("day", "week", "month", "year"):
        return freshness
    if freshness == "any":
        return ""
    for name, pat in _RECENCY_PATTERNS:
        if pat.search(query):
            return name
    return ""


def is_fresh_intent(query: str, freshness: str) -> bool:
    """是否时效型（决定缓存 TTL 走短档）。"""
    return freshness in ("day", "week") or bool(detect_time_range(query, freshness))


def is_evergreen(query: str) -> bool:
    """是否技术常青型（决定缓存 TTL 走长档）。"""
    return bool(_EVERGREEN_RE.search(query))


def is_tech_query(query: str) -> bool:
    """是否技术型（决定是否追加 SearXNG `it` 类目：github/stackoverflow/hackernews/mdn）。"""
    return bool(_TECH_RE.search(query))


def _keywords(query: str, limit: int = 8) -> list[str]:
    """关键词化：jieba 切中文 + 拉丁 token 直取，丢停用词与单字虚词，保序去重。"""
    tokens: list[str] = []
    for ent in _LATIN_ENTITY_RE.findall(query):
        if ent.lower() not in _EN_STOPWORDS:
            tokens.append(ent)
    if has_cjk(query):
        try:
            import jieba

            jieba.setLogLevel(logging.WARNING)
            cut = list(jieba.cut_for_search(query))
        except Exception as e:  # noqa: BLE001 —— 分词器不可用时退回正则粗切
            logger.warning("jieba 不可用，退回粗切: %s", e)
            cut = _CJK_RUN_RE.findall(query)
        for tok in cut:
            t = tok.strip()
            if len(t) < 2 or t in _ZH_STOPWORDS or t.lower() in _EN_STOPWORDS:
                continue
            if not (has_cjk(t) or t.isalnum()):
                continue
            tokens.append(t)
    else:
        for tok in re.split(r"[^\w.+-]+", query.lower()):
            if len(tok) >= 2 and tok not in _EN_STOPWORDS:
                tokens.append(tok)
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _phrase_span(query: str) -> str:
    """挑出最值得精确匹配的显著短语（模型名/产品名/专有名词优先）。"""
    # 已有引号 → 直接用引号内内容
    quoted = re.findall(r'["“”\']([^"“”\']{3,})["“”\']', query)
    if quoted:
        return max(quoted, key=len).strip()
    latin = _LATIN_ENTITY_RE.findall(query)
    # 含数字或连字符的拉丁串更像版本号/型号/模型名，优先
    versioned = [t for t in latin if re.search(r"\d|-|\+", t)]
    if versioned:
        return max(versioned, key=len)
    runs = [r for r in _CJK_RUN_RE.findall(query) if r not in _ZH_STOPWORDS]
    if runs:
        longest = max(runs, key=len)
        # 去掉尾部虚词（"…的作用" → "…"）
        return re.sub(r"(的|之|与|和|及|在|是)$", "", longest) or longest
    if latin:
        return max(latin, key=len)
    return ""


def build_routes(
    query: str,
    *,
    freshness: str = "auto",
    site: str = "",
    settings: Settings | None = None,
) -> list[Route]:
    """构造互补检索式（最多 4 路）。同一 query 恒定产出同一组路（可复现）。"""
    from websearch.config import settings as default_settings

    cfg = settings or default_settings
    weights = cfg.weight_map
    q = _clean(query)
    if not q:
        return []

    def with_site(s: str) -> str:
        return f"{s} site:{site}" if site else s

    time_range = detect_time_range(q, freshness)
    routes: list[Route] = [
        Route(
            name="original",
            query=with_site(q),
            weight=weights.get("original", 1.0),
            time_range=time_range,
            categories="general,news" if time_range else "general",
            reason="整句原样，吃引擎自身语义与改写",
        )
    ]

    # phrase 路：显著短语精确匹配
    span = _phrase_span(q)
    if span and len(span) >= 3 and span.lower() != q.lower():
        rest = [t for t in _keywords(q) if t.lower() not in span.lower()][:4]
        phrase_q = _clean(f'"{span}"' + (" " + " ".join(rest) if rest else ""))
        # 退化检查：查询本身就是一个引号短语（如 '"vector database"'）时，phrase 路会与
        # original 完全同构。这不只是白占一路预算（一次 SearXNG 调用 + 8s 超时额度），
        # 更糟的是 RRF 会把同一份排名投两票（1.0 + 0.9），让 original 路的排序凭空拿到双倍票权
        if _bare(phrase_q) != _bare(q):
            routes.append(
                Route(
                    name="phrase",
                    query=with_site(phrase_q),
                    weight=weights.get("phrase", 0.9),
                    time_range=time_range,
                    reason=f"精确短语「{span}」压关键词噪声",
                )
            )

    # keyword 路：去虚词关键词串
    kws = _keywords(q)
    kw_q = _clean(" ".join(kws))
    if kw_q and kw_q.lower() != q.lower() and len(kws) >= 2:
        routes.append(
            Route(
                name="keyword",
                query=with_site(kw_q),
                weight=weights.get("keyword", 0.8),
                time_range=time_range,
                reason="去虚词关键词，规避中文分词粒度差异",
            )
        )

    # extra 路：时效收窄（news）/ 英文实体 / 技术类目（it）
    # 三路互斥：一个 extra 名额只用一次，用在当前查询最缺的那种互补性上
    tech = is_tech_query(q)
    latin = [t for t in _LATIN_ENTITY_RE.findall(q) if t.lower() not in _EN_STOPWORDS]
    en_q = _clean(" ".join(dict.fromkeys(latin))) if has_cjk(q) and latin else ""
    if time_range and len(routes) < 4:
        routes.append(
            Route(
                name="extra",
                query=with_site(q),
                weight=weights.get("extra", 0.7),
                time_range=time_range,
                categories="news",
                reason=f"时效路（time_range={time_range}，news 类目）",
            )
        )
    elif en_q and en_q.lower() != q.lower() and len(routes) < 4:
        routes.append(
            Route(
                name="extra",
                query=with_site(en_q),
                weight=weights.get("extra", 0.7),
                categories="general,it" if tech else "general",
                reason="英文实体路，捞英文原始资料" + ("（+it 技术类目）" if tech else ""),
            )
        )
    elif tech and len(routes) < 4:
        routes.append(
            Route(
                name="extra",
                query=with_site(q),
                weight=weights.get("extra", 0.7),
                categories="it",
                reason="技术类目路（github/stackoverflow/hackernews/mdn 独立一票）",
            )
        )
    return routes[:4]
