"""Settings loaded from .env."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    mongodb_uri: str
    mongodb_db: str = "privacy_agent"
    llm_provider: str = "anthropic"
    llm_api_key: str = ""
    llm_model: str = ""
    voyage_api_key: str = ""
    k_threshold: int = 5
    median_threshold: int = 10
    memory_scope: str = "team"   # user | team | org

    class Config:
        env_file = ".env"


settings = Settings()
