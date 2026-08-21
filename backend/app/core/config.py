"""应用配置（pydantic-settings，环境变量 > .env > 默认值）。

env_file 同时指向 backend/.env 与仓库根 .env（make dev 的 cwd 是 backend/）。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_name: str = "Agent Platform"
    database_url: str = (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5432/agent_platform"
    )
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 天
    embedding_dim: int = 1536


settings = Settings()
