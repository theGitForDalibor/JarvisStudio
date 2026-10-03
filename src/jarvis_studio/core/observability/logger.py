"""Structured logging configuration for Jarvis Studio using Loguru."""

import sys
from pathlib import Path
from loguru import logger

# Remove default handler
logger.remove()

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
)


def setup_logger(log_level: str = "INFO", log_file: str = "logs/jarvis_studio.log") -> None:
    """Initialize structured console and rotating file logger."""
    # Ensure logs directory exists
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    # Console output
    logger.add(
        sys.stderr,
        format=_LOG_FORMAT,
        level=log_level,
        colorize=True,
    )

    # Rotating file output
    logger.add(
        log_file,
        format=_LOG_FORMAT,
        level=log_level,
        rotation="10 MB",
        retention="14 days",
        compression="zip",
        encoding="utf-8",
    )


# Default initialization
setup_logger()

__all__ = ["logger", "setup_logger"]
