"""应用配置（pydantic-settings，环境变量 > .env > 默认值）。

env_file 同时指向 backend/.env 与仓库根 .env（make dev 的 cwd 是 backend/）。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_name: str = "Agent Platform"
    database_url: str = (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5432/agent_platform"
    )
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 天
    # bge_m3_embed（Kimi 端点实测）：1024 维；换 embedding 模型需统一重建向量索引
    embedding_dim: int = 1024
    # 上下文压缩阈值（粗估 token = chars/3）：达到即触发 assembler L1/L2 压缩
    context_compact_threshold: int = 24000
    # 运行时数据目录（文件存储根，gitignore）；解析容器 REST 地址（docling-serve）
    data_dir: Path = Path("data")
    parser_url: str = "http://localhost:5001"
    # LLM 统一网关（LLM_Gateway）：所有模型经网关路由，平台不直连上游；
    # 真实上游密钥由网关集中管理（providers.yaml），平台只持有网关 key
    llm_gateway_base_url: str = "http://localhost:18080/v1"
    llm_gateway_api_key: str = "local-demo-key"
    # 采购报价对比 Agent（决策4：保持外部服务，平台经标准 REST 契约薄桥接，§5）；
    # 其决策页作为 plugin 前端（iframe）由侧边栏加载，基址同源
    procurement_agent_base_url: str = "http://localhost:8100"
    # 第三方开发者开放注册接口（/api/v1/open）静态令牌；None = 开放接口停用（503）
    open_api_token: str | None = None
    # 自建网页搜索服务（MCP Streamable HTTP，容器隔离区，§7.3）：
    # 五级漏斗（召回→融合→重排→抽取→缓存）全部在 services/websearch 内完成，
    # 平台侧只登记一个 type=mcp 能力并按标准通道消费，核心进程零新依赖。
    # 8204 而非更顺手的 8200：后者在部分开发机上被桌面外设常驻进程占着且 accept 不响应，
    # 探测会白等满 timeout 再把能力误判为 unhealthy（与 compose 的 SEARCH_API_PORT 成对修改）
    search_mcp_url: str = "http://localhost:8204/mcp"
    # REST 基址：seed 用它探可达性（决定 enabled/health_status），不承载工具调用
    search_rest_base_url: str = "http://localhost:8204"
    # 探测超时：必须短——它在 lifespan 里同步执行，服务没起时不能拖慢启动
    search_probe_timeout: float = 3.0
    # run 全局超时缺省值（秒）：run 创建时的 budget 快照 > Agent 配置 > 此值（方案 §4 P0-1）。
    # 超时以绝对截止时间 deadline_at 表达，分段执行（interrupt/resume）不重置。
    run_default_timeout_seconds: int = 600
    # 暂停（等用户确认/等外部回调）对截止时间的顺延上限（秒）：
    # 超出部分不再顺延，避免"次日才确认"使超时保护形同虚设
    max_run_pause_seconds: int = 3600
    # 连续外部/解析失败熔断阈值（方案 §4 P0-3 第二道闸）：
    # 同一 run 内连续 N 次 external_unavailable / parse_error → 结束 run，
    # error.code=tool_failure_loop，不进入终答。run.get("tool_failure_limit") 可覆盖
    tool_failure_limit: int = 3
    # 单条工具观察 / 终答超过此字符数即自动落 run_artifacts，上下文与 result.text
    # 只保留摘要 + 产物引用（方案 §4 P0-5：长结果不再被 120 字符折叠折成一句话）
    artifact_inline_max_chars: int = 4000
    # 外置后留在上下文/终答里的预览字符数（引用行不计入）
    artifact_preview_chars: int = 800


settings = Settings()
