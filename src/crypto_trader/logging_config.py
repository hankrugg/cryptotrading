"""Console and rotating-file logging configuration."""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys


def configure_logging() -> None:
    # The path can be overridden in .env; otherwise logs live below the repo.
    log_file = Path(os.getenv("CRYPTO_TRADER_LOG_FILE", "logs/crypto-trader.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Both the console and file handlers use the same short, readable format.
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Rotate at 10 MB and keep five old files so a stuck service cannot fill
    # the Raspberry Pi's disk indefinitely.
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    # systemd captures stdout in journald, so this handler makes logs visible
    # through ``journalctl`` as well as in the rotating file.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    # force=True replaces handlers left behind by libraries or earlier setup.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[file_handler, console_handler],
        force=True,
    )
