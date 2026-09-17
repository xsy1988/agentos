"""评测体系的纯指标数学（无 IO、无网络，可单测手算对照）。

## 口径纪律：每个指标都写明它**测不到**什么

指标最大的风险不是算错，而是被当成「质量胜利」来读。这里逐条交代局限：

- `recall@50`：候选窗口内是否存在 ≥1 个**已标注**相关项。金标集必然不完备
  （没标注的相关页会被算成不相关）→ 实测值系统性偏低。它的用途是**跨版本对比**，
  不是绝对质量分。
- `nDCG@10`：分级相关（2/1/0）。IDCG 由**标注集合**算出（每条标注占一个理想位次），
  所以只标了 `domain + grade:1` 的条目，即便排到第一也只能得 1.0 的 DCG 贡献，
  而 `url + grade:2` 的条目理想贡献是 3.0。这是不完备标注下的标准做法，
  代价是绝对值偏低；**标注覆盖率**（grade2 条目占比）必须与指标一起读。
- `route_agreement`：top10 中出现在 ≥2 路的占比。**结构指标，不是质量指标**——
  加权 RRF 天然把多路共识的文档推到前面，实测常年接近 1.0。它只在消融对照里
  （单路 vs 多路）有判别力，单看一次跑分不能说明任何事。
- `span_hit`：输出片段是否含 `answer_spans` 之一。**自动判定子集，不依赖人工标注**，
  因此是回归门禁里最可信的一项：它直接回答「模型拿到的片段里有没有那个事实」。
- `MRR`：首个 grade≥1 的倒数排名。与 nDCG 一样受标注完备性影响，但对「第一条就对了」
  这件事更敏感——Agent 常常只读前一两条。

## 判定匹配规则

`url` 精确匹配与 `domain` 后缀匹配都走**归一化后**的形式（与流水线同一把尺子：
`normalize_url`），否则 yaml 里写 `https://X.com/a/` 而流水线给出 `https://x.com/a`
就会假阴性。同一文档命中多条规则时取**最高分**。
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from websearch.cache import percentile  # 与服务端 /metrics 同一分位口径，不另立一套
from websearch.normalize import normalize_url

__all__ = [
    "Judgment",
    "bare_domain",
    "dcg",
    "grade_of",
    "ideal_dcg",
    "matched_spans",
    "mean",
    "ndcg_at_k",
    "parse_judgments",
    "percentile",
    "recall_at_k",
    "route_agreement",
    "span_hit",
]


@dataclass(frozen=True)
class Judgment:
    """一条人工判定：`url` 精确 或 `domain` 后缀，二者至少一个；grade ∈ {1,2}。"""

    grade: int = 1
    url: str = ""  # 已归一化
    domain: str = ""  # 已归一化为裸域（小写、无 scheme、无路径）
    note: str = ""  # 标注依据（实际打开确认过 / 仅按权威域名推断），写进报告便于复查


def bare_domain(raw: str) -> str:
    """`https://GitHub.com/docs/x` → `github.com`。

    标注时很容易顺手粘一整条 URL 进 `domain` 字段；不纠正就会永远匹配不上，
    而且**不报错**——只会让 recall/nDCG 静默变低。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        return (urlsplit(text).hostname or "").strip(".")
    return text.split("/")[0].split("?")[0].strip(".").strip()


def parse_judgments(raw: Any) -> list[Judgment]:
    """yaml 的 `relevant:` 列表 → Judgment 列表。任何不合规都**当场报错**。

    宁可让评测跑不起来，也不要让一条写错的标注把指标悄悄拉低——后者会让人
    去改流水线参数「修复」一个根本不存在的质量问题。
    """
    out: list[Judgment] = []
    for item in raw or []:
        if not isinstance(item, dict):
            raise ValueError(f"relevant 条目必须是映射，实际 {type(item).__name__}: {item!r}")
        try:
            grade = int(item.get("grade", 1))
        except (TypeError, ValueError) as e:
            raise ValueError(f"grade 必须是整数，实际 {item.get('grade')!r}") from e
        if grade not in (1, 2):
            raise ValueError(f"grade 只支持 1（相关）/ 2（直接回答），实际 {grade}")
        url = str(item.get("url") or "").strip()
        domain = bare_domain(item.get("domain") or "")
        if not url and not domain:
            raise ValueError(f"relevant 条目必须给 url 或 domain 之一：{item!r}")
        out.append(
            Judgment(
                grade=grade,
                url=normalize_url(url) if url else "",
                domain=domain,
                note=str(item.get("note") or "").strip(),
            )
        )
    if not out:
        raise ValueError("relevant 不能为空：没有标注的条目产不出任何指标")
    return out


def _host_of(norm_url: str) -> str:
    return (urlsplit(norm_url).hostname or "").lower()


def grade_of(norm_url: str, judgments: Sequence[Judgment]) -> int:
    """该 URL 的相关度等级（0 = 未标注/不相关）。命中多条规则取最高分。"""
    if not norm_url:
        return 0
    host = _host_of(norm_url)
    best = 0
    for j in judgments:
        hit_url = bool(j.url) and j.url == norm_url
        hit_domain = bool(j.domain and host) and (
            host == j.domain or host.endswith(f".{j.domain}")
        )
        if hit_url or hit_domain:
            best = max(best, j.grade)
    return best


def grade_list(urls: Sequence[str], judgments: Sequence[Judgment]) -> list[int]:
    return [grade_of(u, judgments) for u in urls]


def recall_at_k(
    window_urls: Sequence[str], judgments: Sequence[Judgment], k: int = 50
) -> float:
    """窗口内存在 ≥1 个 grade≥1 → 1.0，否则 0.0（单条 query 是二值，聚合后即命中率）。"""
    top = window_urls[:k] if k > 0 else list(window_urls)
    return 1.0 if any(grade_of(u, judgments) >= 1 for u in top) else 0.0


def dcg(grades: Sequence[int]) -> float:
    """标准分级 DCG：Σ (2^g − 1) / log2(rank + 1)，rank 从 1 起。"""
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ideal_dcg(judgments: Sequence[Judgment], k: int = 10) -> float:
    """IDCG：把标注集合按 grade 降序排满前 k 位。

    每条标注只占**一个**理想位次（domain 标注实际可能匹配多篇，但没人逐篇确认过，
    多给位次等于凭空造出「本该有更多相关页」的假设，把 nDCG 压低到无法解释）。
    """
    ideal = sorted((j.grade for j in judgments), reverse=True)[:k]
    return dcg(ideal)


def ndcg_at_k(result_urls: Sequence[str], judgments: Sequence[Judgment], k: int = 10) -> float:
    """分级 nDCG@k。无标注或 IDCG=0 时返回 0.0（不返回 1.0——那会把没标注的
    query 洗成满分，平均分立刻失真）。"""
    idcg = ideal_dcg(judgments, k)
    if idcg <= 0:
        return 0.0
    got = dcg(grade_list(result_urls[:k], judgments))
    return round(got / idcg, 4)


def mrr(result_urls: Sequence[str], judgments: Sequence[Judgment]) -> float:
    """首个 grade≥1 的倒数排名；一个都没命中则 0.0。"""
    for i, url in enumerate(result_urls):
        if grade_of(url, judgments) >= 1:
            return round(1.0 / (i + 1), 4)
    return 0.0


def route_agreement(
    top_urls: Sequence[str], route_lists: dict[str, Sequence[str]], min_routes: int = 2
) -> float:
    """top-N 中出现在 ≥min_routes 路的 URL 占比。

    只在**多路**时有意义：消融到单路时它恒为 0，不是质量下降，是定义如此
    （调用方应把单路跑分的这一列标记为 n/a 而不是 0.0）。
    """
    if not top_urls:
        return 0.0
    sets = [set(urls) for urls in route_lists.values()]
    if len(sets) < min_routes:
        return 0.0
    hits = sum(1 for u in top_urls if sum(1 for s in sets if u in s) >= min_routes)
    return round(hits / len(top_urls), 4)


def _squash(text: str) -> str:
    """小写 + 去掉所有空白。

    答案串常被排版拆开（`568 M`、`8 192 tokens`、全角空格），按原文子串匹配会假阴性，
    而这是自动判定子集唯一的信号来源。代价是短数字串可能落进更长的数字里
    （`8192` 命中 `181923`）——所以 `answer_spans` 要尽量写**带单位/带上下文**的串。
    """
    return "".join(str(text or "").split()).lower()


def matched_spans(passages: Sequence[str], answer_spans: Sequence[str]) -> list[str]:
    """返回实际命中的 span 列表（写进报告，便于人工复查是不是假阳性）。"""
    blob = _squash("\n".join(passages))
    if not blob:
        return []
    return [s for s in answer_spans if _squash(s) and _squash(s) in blob]


def span_hit(passages: Sequence[str], answer_spans: Sequence[str]) -> bool:
    """任一 span 命中即算成功（片段召回率的判定）。无 span 标注的条目由调用方跳过。"""
    return bool(matched_spans(passages, answer_spans))


def mean(values: Sequence[float | None]) -> float:
    """算术平均，跳过 None，空集返回 0.0。

    签名收 `float | None` 而不是 `float`：调用方（eval.run 的聚合）手里的列表天然
    混着「这一档测不到」的 None（单路时的 route_agreement、无 answer_spans 时的片段命中）。
    让类型如实承认这一点，比在每个调用点写 `# type: ignore` 或先过滤一遍更省事，
    也不会出现「过滤漏了一处 → None 被当成 0 参与平均」的静默失真。
    """
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else 0.0
