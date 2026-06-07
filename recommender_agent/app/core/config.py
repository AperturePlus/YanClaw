from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "LightGraphRec"
    APP_ENV: str = "local"
    DEBUG: bool = False
    DATABASE_URL: str = "sqlite:///data/app.db"
    SCU_SOURCE_DB: str = "scu.edu.cn.db"
    SOURCE_CONFIG: str = "configs/sources.yaml"
    SOURCE_DB_DIR: str = "raw_data"
    SOURCE_DB_GLOB: str = "*.db"
    SOURCE_DBS: str | None = None
    DATA_DIR: str = "data"
    PIPELINE_REPORT_DIR: str = "data/pipeline_reports"
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
    ENABLE_DOMAIN_GATE: bool = True
    DOMAIN_GATE_MODE: str = "soft"
    DOMAIN_GATE_MIN_CONFIDENCE: float = 0.3
    DOMAIN_GATE_FALLBACK_ON_EMPTY: bool = True
    DOMAIN_ONTOLOGY_PATH: str = "configs/discipline_ontology.yaml"
    ENABLE_RERANKER: bool = False
    RERANK_PROVIDER: str = "heuristic"
    RERANK_MODEL: str = ""
    RERANK_BASE_URL: str = ""
    RERANK_API_KEY: str = ""
    RERANK_TOP_N: int = 50
    RERANK_TIMEOUT_SECONDS: float = 30.0
    RERANK_WEIGHT: float = 0.20
    DOMAIN_WEIGHT: float = 0.15
    SEMANTIC_WEIGHT: float = 0.35
    GRAPH_WEIGHT: float = 0.25
    PROFILE_WEIGHT: float = 0.20
    POPULARITY_WEIGHT: float = 0.10
    FRESHNESS_WEIGHT: float = 0.10
    GRAPH_DIFFUSION_ENABLED: bool = True
    GRAPH_RELATION_WEIGHTS: str = ""
    GRAPH_MAX_DEPTH: int = 2
    GRAPH_DEPTH_DECAY: float = 0.5
    GRAPH_MAX_CANDIDATES: int = 200

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, value: object) -> object:
        if isinstance(value, str) and value.lower() in {"release", "production", "prod"}:
            return False
        return value

    class Config:
        env_file: str = ".env"
        env_file_encoding: str = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
