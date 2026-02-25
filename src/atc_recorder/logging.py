"""Logging configuration for ATC Recorder."""

import logging
import os
import sys
from pathlib import Path
from typing import Optional


def setup_logging(
    level: Optional[str] = None,
    log_file: Optional[Path] = None,
    log_format: Optional[str] = None,
) -> logging.Logger:
    """Configure logging for ATC Recorder.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
               Defaults to ATC_LOG_LEVEL env var or INFO.
        log_file: Optional file to write logs to.
        log_format: Optional custom log format.

    Returns:
        Configured logger instance.
    """
    # Get log level from environment or parameter
    if level is None:
        level = os.environ.get("ATC_LOG_LEVEL", "INFO")

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Default format includes timestamp for Docker/production use
    if log_format is None:
        log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # Configure root logger
    logging.basicConfig(
        level=numeric_level,
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[],
    )

    # Get our logger
    logger = logging.getLogger("atc_recorder")
    logger.setLevel(numeric_level)

    # Clear existing handlers
    logger.handlers.clear()

    # Console handler (stderr for Docker compatibility)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "atc_recorder") -> logging.Logger:
    """Get a logger instance.

    Args:
        name: Logger name (defaults to atc_recorder).

    Returns:
        Logger instance.
    """
    return logging.getLogger(name)


# Module-level logger for convenience
logger = get_logger()
