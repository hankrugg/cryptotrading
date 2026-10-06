"""Email delivery for strategy targets."""

from email.message import EmailMessage
import logging
import os
import smtplib
import ssl

logger = logging.getLogger(__name__)


def send_email_notification(signal, symbol):
    """Send an email notification with the trading signal."""
    address = "hankrugg2@gmail.com"
    password = os.environ["GMAIL_APP_PASSWORD"]
    side = "long" if signal > 0 else "short"
    target = f"{abs(signal):.1%} {side}" if signal != 0 else "cash (close position)"
    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = f"{symbol} target: {target}"
    message.set_content(f"Target allocation: {target} in {symbol}.\n")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as server:
        server.login(address, password)
        server.send_message(message)
    logger.info(f"Email notification sent: {target}")
