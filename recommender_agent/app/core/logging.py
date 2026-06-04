import sys
from loguru import logger


def configure_logging():
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | <cyan>{extra[request_id]}</cyan> | <white>{message}</white>",
        level="INFO",
        colorize=True,
    )
    logger.add(
        "logs/app.log",
        rotation="10 MB",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {extra[request_id]} | {message}",
        level="DEBUG",
    )


def get_logger(request_id: str = ""):
    return logger.bind(request_id=request_id)
