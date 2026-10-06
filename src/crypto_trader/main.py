import logging
from logging.handlers import RotatingFileHandler

import os
import sys
import smtplib
import time
import ssl
from email.message import EmailMessage
import pandas as pd


from io import StringIO
from pathlib import Path
from dotenv import load_dotenv



from crypto_trader.data import fetch_hourly
from crypto_trader.strategies.sma_crossover import sma_distance

SYMBOL = "SOL-USD"

# load the OS env
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

def configure_logging() -> None:
    log_file = Path(
        os.getenv(
            "CRYPTO_TRADER_LOG_FILE",
            "logs/crypto-trader.log",
        )
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,              # Keep five older files
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[file_handler, console_handler],
        force=True,
    )


def send_email_notification(signal):
    """Send an email notification with the trading signal."""
    address = "hankrugg2@gmail.com"
    password = os.environ["GMAIL_APP_PASSWORD"]
    side = "long" if signal > 0 else "short"
    target = f"{abs(signal):.1%} {side}" if signal != 0 else "cash (close position)"

    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = f"{SYMBOL} target: {target}"
    message.set_content(f"Target allocation: {target} in {SYMBOL}.\n")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as server:
        server.login(address, password)
        server.send_message(message)
    logger.info(f"Email notification sent: {target}")

def run_trading_signal():
    # connect to data provider to pull data
    data = fetch_hourly()

    prices = pd.read_json(StringIO(data[SYMBOL]), orient="table")

    # run data through processing pipeline
    signals = sma_distance(prices, window=20, scale=10)

    # produce signal
    current_signal = signals.iloc[-1]
    if pd.isna(current_signal):
        logger.warning("Current signal is NaN, skipping email notification")
        return

    logger.info(f"Current signal: {current_signal}")

    # send email notification with signal
    send_email_notification(current_signal)

logger = logging.getLogger(__name__)

def main():

    configure_logging()
    logger.info("Crypto trader starting")

    # run every hour and send email
    while True:
        try:
            run_trading_signal()
        except KeyboardInterrupt:
            logger.info("Crypto trader stopped by user")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Error occurred: {e}")
        time.sleep(3600)  # Sleep for 1 hour


if __name__ == "__main__":
    main()
