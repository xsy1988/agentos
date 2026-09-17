"""归一化层单测：URL 归一化、站点标签、近重复合并。

这一层没有算法含量，却是融合层的地基——**认不出同一个页面**，共识票就被稀释、
候选窗口被同一篇文章的五个变体占满，后面再精巧的重排也救不回来。
所以每条规则都要有一个「输入 → 期望输出」的对照断言。
"""

from websearch.normalize import merge_near_duplicates, normalize_url, site_label
from websearch.types import Doc


def _doc(url: str, title: str = "", score: float = 0.0) -> Doc:
    norm = normalize_url(url)
    return Doc(url=url, norm_url=norm, title=title, site=site_label(norm), score=score)


# ---------------------------------------------------------------- normalize_url
def test_strips_tracking_params_and_fragment() -> None:
    assert (
        normalize_url("https://example.com/a?utm_source=x&utm_medium=y&id=7#section")
        == "https://example.com/a?id=7"
    )


def test_keeps_meaningful_params_and_sorts_them() -> None:
    """参数顺序不同也算同一 URL；非跟踪参数一个都不能丢（丢了就指向另一页）。"""
    a = normalize_url("https://example.com/s?q=vector&lang=zh")
    b = normalize_url("https://example.com/s?lang=zh&q=vector")
    assert a == b == "https://example.com/s?lang=zh&q=vector"


def test_http_upgraded_to_https() -> None:
    assert normalize_url("http://example.com/p") == "https://example.com/p"


def test_host_lowercased_and_mobile_prefix_merged() -> None:
    for host in ("EXAMPLE.COM", "m.example.com", "mobile.example.com", "www.example.com"):
        assert normalize_url(f"https://{host}/p") == "https://example.com/p"


def test_trailing_slash_and_double_slash_collapsed() -> None:
    assert normalize_url("https://example.com/a/b/") == "https://example.com/a/b"
    assert normalize_url("https://example.com/a//b") == "https://example.com/a/b"
    # 根路径不留 "/"（否则 https://x.com 与 https://x.com/ 算两条）
    assert normalize_url("https://example.com/") == "https://example.com"


def test_unwraps_search_engine_redirect() -> None:
    """引擎跳转包装必须剥掉：否则 top10 里会出现三条 google.com/url?q=… 占名额。"""
    wrapped = "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fdoc&sa=U&ved=2ahU"
    assert normalize_url(wrapped) == "https://example.com/doc"


def test_redirect_without_target_kept_as_is() -> None:
    """解不出目标时保留原样，不能返回空串把候选直接丢掉。"""
    assert normalize_url("https://t.co/abc123") == "https://t.co/abc123"
    assert normalize_url("https://www.google.com/search?q=x") == "https://google.com/search?q=x"


def test_schemeless_url_gets_https() -> None:
    assert normalize_url("example.com/p") == "https://example.com/p"


def test_empty_and_blank_input() -> None:
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""


def test_short_host_not_over_stripped() -> None:
    """`m.` 前缀只在后面还有真域名时才剥：m.io 这种短域不能被剥成 io。"""
    assert normalize_url("https://m.io/x") == "https://m.io/x"


# ---------------------------------------------------------------- site_label
def test_site_label_simple_and_subdomain() -> None:
    assert site_label("https://example.com/a") == "example.com"
    # 子域是有信息量的（docs. 与 blog. 是不同来源），不能一并抹掉
    assert site_label("https://docs.example.com/a") == "docs.example.com"


def test_site_label_compound_tld() -> None:
    """复合二级域不能只取最后两段，否则 www.example.com.cn 会变成 com.cn。"""
    assert site_label("https://www.example.com.cn/a") == "example.com.cn"
    assert site_label("https://news.bbc.co.uk/a") == "bbc.co.uk"


def test_site_label_strips_www_and_mobile() -> None:
    assert site_label("https://www.example.com/a") == "example.com"
    assert site_label("https://m.example.com/a") == "example.com"


# ---------------------------------------------------------------- 近重复合并
def test_merge_keeps_highest_score_and_unions_evidence() -> None:
    a = _doc("https://example.com/blog/post", title="向量数据库原理", score=0.30)
    b = _doc("https://example.com/blog/post?from=share", title="向量数据库原理", score=0.55)
    a.engines, b.engines = ["google"], ["bing"]
    a.routes, b.routes = {"original": 3}, {"keyword": 1}
    a.date = ""
    b.date = "2026-03-01"

    out = merge_near_duplicates([a, b])
    assert len(out) == 1
    merged = out[0]
    assert merged.score == 0.55  # 取高
    assert merged.engines == ["bing", "google"]  # 并集且有序（结果可复现）
    assert merged.routes == {"original": 3, "keyword": 1}  # 两路的票都保留
    assert merged.date == "2026-03-01"  # 元数据取更全的那份


def test_merge_route_rank_keeps_the_better_one() -> None:
    """同一路里的两个变体：排名取更靠前的，否则 RRF 会给同一页面投两票。"""
    a = _doc("https://example.com/blog/post", title="同一篇文章")
    b = _doc("https://example.com/blog/post/amp", title="同一篇文章")
    a.routes = {"original": 5}
    b.routes = {"original": 2}
    merged = merge_near_duplicates([a, b])[0]
    assert merged.routes == {"original": 2}


def test_merge_takes_longer_snippet() -> None:
    a = _doc("https://example.com/blog/post", title="同一篇文章")
    b = _doc("https://example.com/blog/post/amp", title="同一篇文章")
    a.snippet = "短摘要"
    b.snippet = "这是一条明显更完整的摘要，包含更多可用于判别的文字"
    assert merge_near_duplicates([a, b])[0].snippet == b.snippet


def test_merge_requires_same_title_and_path_prefix() -> None:
    """标题不同 = 不同文章；一级路径不同 = 不同栏目。两种情况都不许合并。"""
    docs = [
        _doc("https://example.com/blog/post-a", title="向量数据库原理"),
        _doc("https://example.com/blog/post-b", title="倒排索引原理"),
        _doc("https://example.com/docs/post-a", title="向量数据库原理"),
    ]
    assert len(merge_near_duplicates(docs)) == 3


def test_merge_title_key_ignores_punctuation_and_case() -> None:
    """同一标题带不带标点/空格/大小写差异，都该判成一个页面。"""
    a = _doc("https://example.com/blog/x", title="Vector DB: 原理")
    b = _doc("https://example.com/blog/y", title="vector db 原理")
    assert len(merge_near_duplicates([a, b])) == 1


def test_merge_ignores_locale_path_segment() -> None:
    """文档站的 /zh-CN/ 与 /en-US/ 是同一篇文章的两个语言版本。

    实测：MDN 的 429 页面以两个 URL 同时被召回、标题一模一样，
    在 top10 里白白占了两个名额（给模型看一份就够）。
    """
    title = "429 Too Many Requests - HTTP - MDN Web Docs"
    a = _doc("https://developer.mozilla.org/zh-CN/docs/Web/HTTP/Status/429", title=title, score=0.4)
    b = _doc("https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429", title=title, score=0.6)
    out = merge_near_duplicates([a, b])
    assert len(out) == 1
    assert out[0].score == 0.6  # 保留融合分高的那份


def test_merge_locale_rule_does_not_swallow_the_root_page() -> None:
    """`site.com/en` 不能被剔成 `site.com`：那是两个不同页面（英文首页 vs 首页）。"""
    docs = [
        _doc("https://example.com/en", title="Example"),
        _doc("https://example.com", title="Example"),
    ]
    assert len(merge_near_duplicates(docs)) == 2


def test_merge_path_prefix_is_case_insensitive() -> None:
    """路径大小写差异不影响去重判定（URL 本身大小写敏感，但同一标题+同一栏目就是同一篇）。"""
    docs = [
        _doc("https://example.com/Docs/intro", title="安装指南"),
        _doc("https://example.com/docs/intro", title="安装指南"),
    ]
    assert len(merge_near_duplicates(docs)) == 1


def test_merge_same_locale_different_article_not_merged() -> None:
    """语言段相同但标题不同 = 不同文章，不许合并。"""
    docs = [
        _doc("https://example.com/zh-cn/docs/a", title="向量数据库"),
        _doc("https://example.com/zh-cn/docs/b", title="倒排索引"),
    ]
    assert len(merge_near_duplicates(docs)) == 2


def test_merge_without_title_falls_back_to_norm_url() -> None:
    """无标题时退化为按归一化 URL 去重：不能因为缺标题就把所有无标题页并成一条。"""
    docs = [
        _doc("https://example.com/a?utm_source=x"),
        _doc("https://example.com/a"),
        _doc("https://example.com/b"),
    ]
    out = merge_near_duplicates(docs)
    assert len(out) == 2
    assert {d.norm_url for d in out} == {"https://example.com/a", "https://example.com/b"}


def test_merge_preserves_input_order() -> None:
    """合并后仍按输入序（融合分降序）——重排前的顺序就是候选的先验。"""
    docs = [
        _doc("https://a.com/1", title="一"),
        _doc("https://b.com/2", title="二"),
        _doc("https://a.com/1?ref=x", title="一"),
    ]
    out = merge_near_duplicates(docs)
    assert [d.norm_url for d in out] == ["https://a.com/1", "https://b.com/2"]
