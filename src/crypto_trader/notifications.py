"""Email delivery for strategy targets and the current paper-trade report."""

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
    # The risk service may not have run yet, so an absent snapshot is reported
    # explicitly instead of making up a safe-looking value.
    if snapshot is None:
        return "Risk status: unavailable (the minute risk monitor has not recorded a snapshot yet)."
    # Delta is shown relative to equity because the policy is expressed as a
    # multiple of portfolio equity (for example, 2.00x).
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
    # This is currently a single hard-coded Gmail mailbox. The password is read
    # from the environment rather than committed to source control.
    address = "hankrugg2@gmail.com"
    password = os.environ["GMAIL_APP_PASSWORD"]

    # The signed signal is converted into the human-readable target shown in
    # the subject and message body.
    side = "long" if signal > 0 else "short"
    target = f"{abs(signal):.1%} {side}" if signal != 0 else "cash (close position)"
    if connection is not None:
        # Export the entire trade table, not just trades created this hour.
        trade_csv = export_trades_csv(connection)
        if risk_snapshot is None:
            # If the caller did not provide a snapshot, use the most recent
            # one recorded by the minute risk service.
            risk_snapshot = latest_risk_snapshot(connection)
    else:
        trade_csv = ""
    risk_status = risk_snapshot.status if risk_snapshot is not None else "unavailable"
    # Build a normal MIME email. The CSV is an attachment only when a database
    # connection was supplied, which keeps this helper testable in isolation.
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
    # SMTP_SSL encrypts the connection to Gmail. send_message does the actual
    # network delivery and can raise if the password, network, or SMTP service
    # is unavailable.
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as server:
        server.login(address, password)
        server.send_message(message)
    logger.info(f"Email notification sent: {target}")
