from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agents.crawler.url_heuristics import DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS


class CrawlerSettings(BaseSettings):
    """Configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="YANCLAW_",
        extra="ignore",
    )

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.0, ge=0.0)
    llm_top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    llm_seed: int | None = None
    llm_max_concurrent: int = Field(default=8, ge=1)
    llm_min_interval_seconds: float = Field(default=0.0, ge=0.0)
    max_concurrency: int = Field(default=3, ge=1)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    university_timeout_seconds: float = Field(default=36000.0, gt=0)
    model_max_tokens: int = Field(default=128000, gt=0)
    response_reserved_tokens: int = Field(default=2000, ge=0)
    log_dir: Path = Path("logs")
    runtime_skills_dir: Path = Path("src/runtime/skills")
    crawler_skills_dir: Path = Path("src/agents/crawler/skills")
    data_steward_skills_dir: Path = Path("src/agents/data_steward/skills")
    websites_path: Path = Path("assets/websites.md")
    university_db_dir: Path = Path("data/universities")
    knowledge_graph_db_path: Path = Path("data/knowledge_graph.db")
    recommend_top_schools: int = Field(default=5, ge=1)
    recommend_top_org_units: int = Field(default=10, ge=1)
    recommend_top_professors: int = Field(default=20, ge=1)
    human_server_host: str = "127.0.0.1"
    human_server_port: int = 21520
    human_job_timeout_seconds: float = 60.0
    detail_enrich_enabled: bool = True
    detail_profile_hard_cap_per_org_unit: int = Field(default=200, ge=1)
    pipeline_enabled: bool = True
    pipeline_llm_workers: int = Field(default=4, ge=1)
    pipeline_db_workers: int = Field(default=1, ge=1)
    pipeline_queue_cap: int = Field(default=64, ge=1)
    invalid_json_max_retry: int = Field(default=1, ge=0)
    task_recovery_enabled: bool = True
    target_org_units: list[str] = Field(default_factory=list)
    org_unit_match_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    org_unit_exclude_enabled: bool = True
    org_unit_exclude_keywords: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS)
    )
    org_unit_llm_filter_enabled: bool = True
