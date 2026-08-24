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


settings = Settings()
