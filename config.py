from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8")
    app_name: str = "AI Hosting Platform"
    environment: str = "development"
    api_host: str = "0.0.0.0"
    
    api_port: int = 8000
    # External URL used by VPS bootstrap jobs. There is intentionally no localhost fallback.
    public_api_base_url: str = ""
    pinggy_ssh_host: str = "free.pinggy.io"
    pinggy_ssh_port: int = 443
    pinggy_token: str = ""
    pinggy_timeout_seconds: int = 45
    pinggy_health_path: str = "/api/health"
    vault_master_key: str
    database_url: str = "sqlite:///./data/onboarding.db"
    cors_origins: str = "http://localhost:8000"
    github_token: str = ""
    redis_url: str = ""
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    analysis_timeout_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
