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


settings = Settings()
