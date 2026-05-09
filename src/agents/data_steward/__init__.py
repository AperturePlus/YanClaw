"""Data Steward agent for crawler database quality cleanup."""

from runtime.database import DatabaseManager

DatabaseManager.register_models("agents.crawler.models")

