from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, read from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Bulk Certificate Generator"

    # Required: no default, so credentials are never hard-coded in source.
    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    storage_dir: Path = Path("storage")
    max_batch_size: int = 1000
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    # Cached so the environment is parsed once per process.
    return Settings()
