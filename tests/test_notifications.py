"""Tests for email formatting without sending a real message."""

from email.message import EmailMessage
from unittest.mock import patch
import os
import sqlite3
import unittest

from crypto_trader.data import create_trades_table
from crypto_trader.notifications import send_email_notification
from crypto_trader.risk import evaluate_risk, record_risk_snapshot


class NotificationTests(unittest.TestCase):
    def test_email_contains_risk_summary_and_csv_attachment(self) -> None:
        # Build a risk row, mock SMTP, then inspect the exact MIME message that
        # would have been sent to Gmail.
        connection = sqlite3.connect(":memory:")
        create_trades_table(connection)
        snapshot = evaluate_risk(
            [],
            equity_usd=100_000,
            day_start_equity_usd=100_000,
            peak_equity_usd=100_000,
            captured_at_ms=1_800_000_000_000,
        )
        record_risk_snapshot(connection, snapshot)
        with patch.dict(os.environ, {"GMAIL_APP_PASSWORD": "test-password"}), patch(
            "crypto_trader.notifications.smtplib.SMTP_SSL"
        ) as smtp:
            send_email_notification(1.0, "SOL-USD", connection=connection)

        message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertIsInstance(message, EmailMessage)
        self.assertIn("Risk status: ok", message.get_body(preferencelist=("plain",)).get_content())
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertTrue(attachments[0].get_filename().startswith("trade_log_"))
        self.assertIn("Symbol,Trade number,Type", attachments[0].get_content())
        connection.close()


if __name__ == "__main__":
    unittest.main()
