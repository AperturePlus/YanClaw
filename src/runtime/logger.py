from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


class _ConsoleFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).strftime("%H:%M:%S")


class _FileFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created).isoformat(timespec="seconds")


def setup_logging(log_dir: str | Path = "logs") -> Path:
    """Configure the yanclaw logger with concise console and detailed file output."""

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_path = log_path / f"crawl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logger = logging.getLogger("yanclaw")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(_ConsoleFormatter("[%(asctime)s] [%(name)s] %(message)s"))

    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        _FileFormatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    )

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return file_path


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the yanclaw namespace."""

    logger_name = name if name == "yanclaw" or name.startswith("yanclaw.") else f"yanclaw.{name}"
    return logging.getLogger(logger_name)
