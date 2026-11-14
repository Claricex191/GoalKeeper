"""Settings loaded from backend/.env (found regardless of the current directory)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    mongodb_uri: str = ""      # empty in CI: importing must work, connecting fails (db.py)
    mongodb_db: str = "privacy_agent"
    llm_provider: str = "anthropic"
    llm_api_key: str = ""
    llm_model: str = ""
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3.5-lite"
    voyage_dims: int = 1024    # must match numDimensions of the precedents_vec index
    # Thresholds, memory scope and retention are NOT settings: they come from the
    # active policy (policies collection), which is versioned and human-approved.


settings = Settings()
