import os
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

def run_trading_signal():
    # connect to data provider to pull data
    data = fetch_hourly()

    prices = pd.read_json(StringIO(data[SYMBOL]), orient="table")

    # run data through processing pipeline
    signals = sma_distance(prices, window=20, scale=10)

    # produce signal
    current_signal = signals.iloc[-1]
    if pd.isna(current_signal):
        return

    # send email notification with signal
    send_email_notification(current_signal)

def main():

    # run every hour and send email
    while True:
        try:
            run_trading_signal()
        except Exception as e:
            print(f"Error occurred: {e}")
        time.sleep(36)  # Sleep for 36 seconds


if __name__ == "__main__":
    main()
