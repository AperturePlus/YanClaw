from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    APP_NAME: str = "LightGraphRec"
    APP_ENV: str = "local"
    DEBUG: bool = True
    DATABASE_URL: str = "sqlite:///data/app.db"
    SCU_SOURCE_DB: str = "scu.edu.cn.db"
    CHROMA_PATH: str = "data/chroma"
    CHROMA_COLLECTION: str = "professors_vector"
    LLM_PROVIDER: str = "deepseek"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT_SECONDS: float = 30.0
    DEFAULT_TOP_K: int = 10
    ENABLE_LLM: bool = True
    ENABLE_VECTOR: bool = True
    ENABLE_GRAPH: bool = True
    SEMANTIC_WEIGHT: float = 0.35
    GRAPH_WEIGHT: float = 0.25
    PROFILE_WEIGHT: float = 0.20
    POPULARITY_WEIGHT: float = 0.10
    FRESHNESS_WEIGHT: float = 0.10
    GRAPH_MAX_DEPTH: int = 3

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
