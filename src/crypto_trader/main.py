import os
import smtplib
import ssl
from email.message import EmailMessage
import pandas as pd
from io import StringIO
from pathlib import Path
from dotenv import load_dotenv


from data import fetch_hourly
from strategies.sma_crossover.strategy import SMACrossover

# load the OS env
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def send_email_notification(signal):
    """Send an email notification with the trading signal."""
    address = "hankrugg2@gmail.com"
    password = os.environ["GMAIL_APP_PASSWORD"]
    action = {1: "BUY", -1: "SELL", 0: "HOLD"}.get(signal, str(signal))

    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = f"Crypto paper trading: {action}"
    message.set_content(f"Your strategy signal is: {action}\nPaper trading only.")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as server:
        server.login(address, password)
        server.send_message(message)
    

def __main__():

    # connect to data provider to pull data
    data = fetch_hourly()

    btc = pd.read_json(StringIO(data["BTC-USD"]), orient="table")


    # run data through processing pipeline
    signals = SMACrossover(btc, fast=5, slow=20)
    signals.calculate_sma()
    signals = signals.generate_signals()

    # produce signal
    current_signal = signals["signal"].iloc[-1]

    # send email notification with signal
    send_email_notification(current_signal)


if __name__ == "__main__":
    __main__()
