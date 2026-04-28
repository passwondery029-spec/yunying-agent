"""云英 AI 配置管理"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # LLM
    llm_provider: str = "doubao"
    llm_api_key: str = ""
    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_model: str = "ep-m-20260305204118-rh2xg"
    llm_fallback_model: str = ""  # 降级模型（如 doubao-seed-1-6-lite-250828）
    llm_extractor_model: str = ""  # 记忆提取用轻量模型（如同flash或lite）

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://yunying:yunying@localhost:5432/yunying"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True

    # Log
    log_level: str = "INFO"

    # Auth
    jwt_secret: str = ""
    jwt_expire_hours: int = 72
    jwt_refresh_days: int = 30

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
