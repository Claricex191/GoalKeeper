"""Settings loaded from backend/.env (found regardless of the current directory)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    mongodb_uri: str
    mongodb_db: str = "privacy_agent"
    llm_provider: str = "anthropic"
    llm_api_key: str = ""
    llm_model: str = ""
    voyage_api_key: str = ""
    k_threshold: int = 5
    median_threshold: int = 10
    memory_scope: str = "team"   # user | team | org


settings = Settings()
