"""Crawler agent implementation."""
from agents.crawler.config import CrawlerSettings
from runtime.database import DatabaseManager

DatabaseManager.register_models("agents.crawler.models")

__all__ = ["CrawlerSettings"]
