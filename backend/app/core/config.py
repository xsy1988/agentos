"""应用配置（pydantic-settings，环境变量 > .env > 默认值）。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Agent Platform"
    database_url: str = (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5432/agent_platform"
    )
    secret_key: str = "change-me-in-production"


settings = Settings()
