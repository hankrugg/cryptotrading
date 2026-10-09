import csv
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from crypto_trader.data.coinbase_ticks import (
    CoinbaseMessageError,
    Level2Book,
    MessageSequenceTracker,
    SequenceGapError,
    normalize_products,
    parse_coinbase_message,
)
from crypto_trader.data.rotating_writer import RotatingTickWriter


def _level2_row(
    *,
    event_type: str,
    received_at_ns: int,
    sequence_num: int,
    side: str = "bid",
    price: str = "100.00",
    quantity: str = "2.5",
) -> dict[str, object]:
    return {
        "exchange": "coinbase",
        "product_id": "BTC-USD",
        "event_type": event_type,
        "event_time": "2026-10-09T12:00:00Z",
        "message_time": "2026-10-09T12:00:00Z",
        "received_at_ns": received_at_ns,
        "side": side,
        "price_level": price,
        "new_quantity": quantity,
        "sequence_num": sequence_num,
        "connection_id": "testconnection",
        "record_source": "exchange",
    }


class CoinbaseTickTests(unittest.TestCase):
    def test_normalize_products_deduplicates_without_reordering(self) -> None:
        self.assertEqual(
            normalize_products([" btc-usd ", "ETH-USD", "BTC-USD"]),
            ("BTC-USD", "ETH-USD"),
        )
        with self.assertRaises(ValueError):
            normalize_products([])

    def test_parse_market_trades_preserves_event_and_receive_time(self) -> None:
        message = {
            "channel": "market_trades",
            "sequence_num": 42,
            "events": [
                {
                    "type": "update",
                    "trades": [
                        {
                            "product_id": "BTC-USD",
                            "trade_id": "123",
                            "time": "2026-10-09T12:00:00.123Z",
                            "price": "60000.00",
                            "size": "0.25",
                            "side": "SELL",
                        }
                    ],
                }
            ],
        }

        parsed = parse_coinbase_message(
            message,
            received_at_ns=123456789,
            connection_id="connection1",
        )

        self.assertEqual(parsed.channel, "market_trades")
        self.assertEqual(parsed.sequence_num, 42)
        self.assertEqual(
            parsed.trade_rows,
            [
                {
                    "exchange": "coinbase",
                    "product_id": "BTC-USD",
                    "trade_id": "123",
                    "exchange_time": "2026-10-09T12:00:00.123Z",
                    "received_at_ns": 123456789,
                    "price": "60000.00",
                    "size": "0.25",
                    "maker_side": "SELL",
                    "event_type": "update",
                    "sequence_num": 42,
                    "connection_id": "connection1",
                    "record_source": "exchange",
                }
            ],
        )

    def test_parse_level2_groups_rows_by_product(self) -> None:
        message = {
            "channel": "l2_data",
            "sequence_num": 7,
            "timestamp": "2026-10-09T12:00:01Z",
            "events": [
                {
                    "type": "snapshot",
                    "product_id": "ETH-USD",
                    "updates": [
                        {
                            "side": "bid",
                            "price_level": "3000.00",
                            "new_quantity": "4.0",
                            "event_time": "2026-10-09T12:00:00.9Z",
                        }
                    ],
                }
            ],
        }

        parsed = parse_coinbase_message(
            message,
            received_at_ns=999,
            connection_id="connection2",
        )

        row = parsed.level2_by_product["ETH-USD"][0]
        self.assertEqual(row["event_type"], "snapshot")
        self.assertEqual(row["event_time"], "2026-10-09T12:00:00.9Z")
        self.assertEqual(row["message_time"], "2026-10-09T12:00:01Z")
        self.assertEqual(row["received_at_ns"], 999)

    def test_book_requires_snapshot_and_removes_zero_quantity(self) -> None:
        book = Level2Book("BTC-USD")
        with self.assertRaisesRegex(CoinbaseMessageError, "before its snapshot"):
            book.apply_rows(
                [_level2_row(event_type="update", received_at_ns=1, sequence_num=1)]
            )

        book.apply_rows(
            [_level2_row(event_type="snapshot", received_at_ns=1, sequence_num=1)]
        )
        self.assertIn("100.00", book.levels["bid"])

        book.apply_rows(
            [
                _level2_row(
                    event_type="update",
                    received_at_ns=2,
                    sequence_num=2,
                    quantity="0",
                )
            ]
        )
        self.assertNotIn("100.00", book.levels["bid"])

    def test_sequence_tracker_rejects_gap(self) -> None:
        tracker = MessageSequenceTracker()
        tracker.observe(10)
        tracker.observe(11)
        with self.assertRaisesRegex(SequenceGapError, "expected 12"):
            tracker.observe(13)

    def test_rotated_level2_file_starts_with_local_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            book = Level2Book("BTC-USD")
            writer = RotatingTickWriter(
                tmp_path,
                connection_id="testconnection",
                rotation_seconds=60,
                flush_seconds=0.01,
            )

            snapshot_rows = [
                _level2_row(
                    event_type="snapshot",
                    received_at_ns=1_000_000_000,
                    sequence_num=10,
                    side="bid",
                    price="100.00",
                    quantity="2.0",
                ),
                _level2_row(
                    event_type="snapshot",
                    received_at_ns=1_000_000_000,
                    sequence_num=10,
                    side="offer",
                    price="101.00",
                    quantity="3.0",
                ),
            ]
            writer.write_level2(
                snapshot_rows,
                checkpoint_factory=lambda product, timestamp: [],
            )
            book.apply_rows(snapshot_rows)

            update_rows = [
                _level2_row(
                    event_type="update",
                    received_at_ns=61_000_000_000,
                    sequence_num=11,
                    side="bid",
                    price="100.00",
                    quantity="2.25",
                )
            ]
            writer.write_level2(
                update_rows,
                checkpoint_factory=lambda product, timestamp: book.checkpoint_rows(
                    received_at_ns=timestamp,
                    connection_id="testconnection",
                ),
            )
            book.apply_rows(update_rows)
            writer.close()

            files = sorted(tmp_path.rglob("*.csv.gz"))
            self.assertEqual(len(files), 2)
            with gzip.open(files[1], "rt", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(
                [row["event_type"] for row in rows],
                ["checkpoint", "checkpoint", "update"],
            )
            self.assertTrue(
                all(row["record_source"] == "local_checkpoint" for row in rows[:2])
            )
            self.assertEqual(rows[-1]["record_source"], "exchange")

            manifest_path = Path(f"{files[1]}.manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["row_count"], 3)
            self.assertEqual(manifest["checkpoint_rows"], 2)
            self.assertEqual(len(manifest["sha256"]), 64)
            self.assertFalse(list(tmp_path.rglob("*.partial")))


if __name__ == "__main__":
    unittest.main()
