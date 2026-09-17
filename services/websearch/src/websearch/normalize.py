"""URL 归一化与近重复合并（纯函数，无 I/O）。

融合层的第一件事不是打分而是「认得出同一个页面」：跟踪参数、移动端前缀、
跳转包装、协议差异都会让同一页面变成多个候选，既浪费窗口名额又稀释共识票。
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from websearch.types import Doc

# 跟踪/会话参数（前缀匹配 + 精确匹配）
_TRACKING_PREFIXES = ("utm_", "pk_", "mc_", "_hs", "igsh", "spm", "cmp", "asid")
_TRACKING_EXACT = {
    "fbclid",
    "gclid",
    "dclid",
    "yclid",
    "msclkid",
    "mkt_tok",
    "ref",
    "ref_src",
    "ref_url",
    "source",
    "share_token",
    "share_from",
    "from",
    "vd_source",
    "spm_id_from",
    "sessionid",
    "sid",
}

# 搜索引擎跳转包装：host → 真实 URL 所在的参数名
_REDIRECT_WRAPPERS = {
    "google.com": "q",
    "www.google.com": "q",
    "google.com.hk": "q",
    "bing.com": "u",
    "www.bing.com": "u",
    "duckduckgo.com": "uddg",
    "l.facebook.com": "u",
    "lm.facebook.com": "u",
    "t.co": "",  # 短链无法本地解包，保留原样
}

# 移动/打印版前缀（同一内容的不同壳）
_MOBILE_PREFIXES = ("m.", "mobile.", "amp.", "www.")

# 路径开头的语言/地区段（BCP 47 的常见形式：zh-CN、zh_cn、en-US、pt-br、ja、zh-Hant）。
# 要求后面还有真路径才剔：否则 site.com/en 会被剔成 site.com，与首页并成一条。
# /go/x、/ai/x 这种非语言的两三字首段会被误判，但合并还要求同 host + 同标题，误伤面很小
_LOCALE_SEG_RE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z0-9]{2,8})?$", re.IGNORECASE)

# 复合二级域（判断「站点名」时不能只取最后一段）
_COMPOUND_TLDS = {
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "ac.cn",
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "com.au",
    "net.au",
    "org.au",
    "co.jp",
    "or.jp",
    "ac.jp",
    "ne.jp",
    "com.tw",
    "org.tw",
    "com.hk",
    "org.hk",
    "co.kr",
    "or.kr",
    "com.sg",
    "co.in",
    "com.br",
    "com.mx",
    "co.za",
}


def _unwrap_redirect(url: str) -> str:
    """剥掉搜索引擎跳转包装，拿到真实目标 URL。"""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host not in _REDIRECT_WRAPPERS:
        return url
    param = _REDIRECT_WRAPPERS[host]
    if not param:
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    target = query.get(param) or ""
    if target.startswith("http"):
        return target
    return url


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_EXACT or any(k.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """归一化：解包跳转 → 小写 host → https → 去移动前缀 → 去跟踪参数与 fragment。

    非法/空 URL 原样返回（融合层据此丢弃）。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    raw = _unwrap_redirect(raw)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    host = (parts.hostname or "").lower()
    if not host:
        return raw
    # 去移动/打印/WWW 前缀（保留裸域）
    for prefix in _MOBILE_PREFIXES:
        if host.startswith(prefix) and len(host) > len(prefix) + 3:
            host = host[len(prefix) :]
            break
    # 参数：丢跟踪项，其余按 key 排序（顺序不同也算同一 URL）
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not _is_tracking(k)]
    query = urlencode(sorted(kept))
    path = parts.path or ""
    # 去尾斜杠（根路径留空）
    path = path.rstrip("/") if path not in ("/", "") else ""
    # 合并连续斜杠
    path = re.sub(r"/{2,}", "/", path)
    return urlunsplit(("https", host, path, query, ""))


def site_label(url: str) -> str:
    """站点显示名：example.com / docs.example.com / example.com.cn。"""
    host = (urlsplit(url).hostname or url).lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    tail = ".".join(labels[-2:])
    if tail in _COMPOUND_TLDS:
        return ".".join(labels[-3:])
    return host


def _title_key(title: str) -> str:
    """标题归一化键：小写 + 去标点空白（近重复判定用）。"""
    return re.sub(r"[\s\W_]+", "", title.casefold(), flags=re.UNICODE)


def _path_prefix(url: str) -> str:
    """近重复判定用的一级路径（小写）。

    开头是**语言/地区段**（zh-CN、en-US、pt_br、ja…）时跳过它：文档站的同一篇文章
    会以 /zh-CN/docs/x 与 /en-US/docs/x 两个 URL 同时被召回，标题一模一样——
    实测 MDN 的 429 页面就因此在 top10 里占了两名额（给模型看一份就够）。
    注意只影响**去重键**，不动 URL 本身：归一化后的地址仍是一个真实可访问的页面。
    """
    seg = [s for s in (urlsplit(url).path or "").split("/") if s]
    if len(seg) > 1 and _LOCALE_SEG_RE.match(seg[0]):
        seg = seg[1:]
    return f"/{seg[0].lower()}" if seg else ""


def merge_near_duplicates(docs: list[Doc]) -> list[Doc]:
    """合并近重复：同标题键 + 同 host 同一级路径（忽略语言段）→ 保留融合分最高者，票与引擎并集。

    典型场景：同一篇文章的桌面版/移动版、带 ?from= 的分享链、目录页与 index.html、
    以及文档站的 /zh-CN/ 与 /en-US/ 两个语言版本。
    """
    best: dict[str, Doc] = {}
    for d in docs:
        if d.title:
            host = urlsplit(d.norm_url).hostname or ""
            key = f"{_title_key(d.title)}|{host}{_path_prefix(d.norm_url)}"
        else:
            # 标题为空时退化为按归一化 URL 去重
            key = d.norm_url
        cur = best.get(key)
        if cur is None:
            best[key] = d
            continue
        # 合并证据：路排名取更靠前的，引擎取并集
        for route, rank in d.routes.items():
            if rank < cur.routes.get(route, 10**9):
                cur.routes[route] = rank
        cur.engines = sorted({*cur.engines, *d.engines})
        cur.score = max(cur.score, d.score)
        if len(d.snippet) > len(cur.snippet):
            cur.snippet = d.snippet
        if not cur.date and d.date:
            cur.date = d.date
    return list(best.values())
