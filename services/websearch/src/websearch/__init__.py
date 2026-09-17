"""websearch —— 自建网页搜索服务（五级质量漏斗）。

召回（SearXNG 并发多路）→ 融合（URL 归一化 + 加权 RRF）→ 重排（cross-encoder）
→ 抽取（正文 + 片段级二次精排）→ 缓存（两层，SQLite）。

出口：MCP Streamable HTTP（/mcp，平台注册为 type=mcp 能力），
REST（/search /fetch /health /metrics）同端口同进程。
"""

__version__ = "0.1.0"
