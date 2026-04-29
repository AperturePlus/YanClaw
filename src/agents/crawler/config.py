from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CrawlerSettings(BaseSettings):
    """Configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="YANCLAW_",
        extra="ignore",
    )

    # database_url is reserved for skill version history (SkillManager rollback/diff).
    # Crawled faculty/org_unit data is stored per-university under university_db_dir.
    database_url: str = "sqlite+aiosqlite:///data/yanclaw_meta.db"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    max_concurrency: int = Field(default=3, ge=1)
    request_interval_seconds: float = Field(default=2.0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    university_timeout_seconds: float = Field(default=1800.0, gt=0)
    model_max_tokens: int = Field(default=128000, gt=0)
    response_reserved_tokens: int = Field(default=2000, ge=0)
    log_dir: Path = Path("logs")
    runtime_skills_dir: Path = Path("src/runtime/skills")
    crawler_skills_dir: Path = Path("src/agents/crawler/skills")
    websites_path: Path = Path("assets/websites.md")
    university_db_dir: Path = Path("data/universities")
    fetcher_backend: str = "hybrid"  # "hybrid" (default), "httpx", "playwright", "curl_cffi", or "crawl4ai"
    crawl4ai_base_url: str = "http://localhost:10086"
    crawl4ai_api_token: str = ""
    crawl4ai_timeout_seconds: float = Field(default=120.0, gt=0)
