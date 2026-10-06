"""Console and rotating-file logging configuration."""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys


def configure_logging() -> None:
    log_file = Path(os.getenv("CRYPTO_TRADER_LOG_FILE", "logs/crypto-trader.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[file_handler, console_handler],
        force=True,
    )
