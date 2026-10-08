"""Email delivery for strategy targets."""

from datetime import datetime, timezone
from email.message import EmailMessage
import logging
import os
import sqlite3
import smtplib
import ssl

from crypto_trader.data import export_trades_csv
from crypto_trader.risk import RiskSnapshot, latest_risk_snapshot

logger = logging.getLogger(__name__)


def _risk_summary(snapshot: RiskSnapshot | None) -> str:
    if snapshot is None:
        return "Risk status: unavailable (the minute risk monitor has not recorded a snapshot yet)."
    delta_multiple = (
        snapshot.delta_exposure_usd / snapshot.equity_usd
        if snapshot.equity_usd
        else 0.0
    )
    violations = ", ".join(snapshot.violations) or "none"
    return "\n".join(
        (
            f"Risk status: {snapshot.status}",
            f"Equity: ${snapshot.equity_usd:,.2f}",
            f"Gross exposure: ${snapshot.gross_exposure_usd:,.2f}",
            f"Delta exposure: {delta_multiple:.2f}x equity",
            f"Liquid exposure: {snapshot.liquid_exposure_pct:.2%}",
            f"Other exposure: {snapshot.other_exposure_pct:.2%}",
            f"Maximum spot leverage: {snapshot.max_spot_leverage:.2f}x",
            f"Maximum futures leverage: {snapshot.max_futures_leverage:.2f}x",
            f"Daily drawdown: {snapshot.daily_drawdown_pct:.2%}",
            f"Maximum drawdown: {snapshot.max_drawdown_pct:.2%}",
            f"Violations: {violations}",
        )
    )


def send_email_notification(
    signal,
    symbol,
    *,
    connection: sqlite3.Connection | None = None,
    risk_snapshot: RiskSnapshot | None = None,
):
    """Email the signal, latest risk metrics, and the complete trade CSV."""
    address = "hankrugg2@gmail.com"
    password = os.environ["GMAIL_APP_PASSWORD"]
    side = "long" if signal > 0 else "short"
    target = f"{abs(signal):.1%} {side}" if signal != 0 else "cash (close position)"
    if connection is not None:
        trade_csv = export_trades_csv(connection)
        if risk_snapshot is None:
            risk_snapshot = latest_risk_snapshot(connection)
    else:
        trade_csv = ""
    risk_status = risk_snapshot.status if risk_snapshot is not None else "unavailable"
    message = EmailMessage()
    message["From"] = address
    message["To"] = address
    message["Subject"] = f"{symbol} target: {target} | risk: {risk_status}"
    body = (
        f"Target allocation: {target} in {symbol}.\n\n"
        f"{_risk_summary(risk_snapshot)}\n\n"
        "The complete trade log is attached as a CSV file."
    )
    message.set_content(body)
    if connection is not None:
        message.add_attachment(
            trade_csv.encode("utf-8"),
            maintype="text",
            subtype="csv",
            filename=f"trade_log_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv",
        )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as server:
        server.login(address, password)
        server.send_message(message)
    logger.info(f"Email notification sent: {target}")
