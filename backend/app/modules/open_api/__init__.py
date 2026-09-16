"""open_api —— 第三方开发者开放注册接口（/api/v1/open）。

面向第三方开发者一键注册 Worker 文件包 + 其依赖的 mcp/tool/plugin/skill 能力：
- 鉴权走静态令牌（settings.open_api_token，X-API-Key 头），与平台用户 JWT 体系隔离；
- 能力注册复用 capabilities 服务（校验 → 落库 → 冒烟 → 通过才启用 → 语义索引）；
- Worker 落盘复用 workers registry（不存在 → ensure_worker 落 v1；演进 → publish_version）。

规范文档：docs/第三方Worker开发与注册规范.md
"""
