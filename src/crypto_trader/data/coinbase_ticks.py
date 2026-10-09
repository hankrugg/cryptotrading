"""Continuously collect Coinbase trades and Level 2 updates.

This module turns the exploratory Coinbase notebook into an unattended service.
It uses only Coinbase's public WebSocket channels and therefore cannot place
orders or access an account.  Parsed rows are streamed into rotating gzip-CSV
files instead of being retained in memory.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import fcntl
import json
import logging
import os
from pathlib import Path
import random
import signal
import time
from typing import Any, TextIO
import uuid

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from crypto_trader.data.rotating_writer import RotatingTickWriter
from crypto_trader.logging_config import configure_logging

logger = logging.getLogger(__name__)

COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "raw" / "coinbase"
DEFAULT_LOCK_PATH = PROJECT_ROOT / "data" / ".collector.lock"


class CoinbaseMessageError(RuntimeError):
    """Raised when a Coinbase message cannot safely update the dataset."""


class SequenceGapError(CoinbaseMessageError):
    """Raised when the connection-wide feed skips a sequence number."""


def normalize_products(products: Sequence[str]) -> tuple[str, ...]:
    """Validate, normalize, and de-duplicate Coinbase product identifiers."""

    normalized: list[str] = []
    seen: set[str] = set()
    for product in products:
        value = product.strip().upper()
        if not value or "-" not in value:
            raise ValueError(f"invalid Coinbase product identifier: {product!r}")
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    if not normalized:
        raise ValueError("at least one Coinbase product is required")
    return tuple(normalized)


def _integer_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass
class ParsedCoinbaseMessage:
    """Normalized records produced by one WebSocket message."""

    channel: str
    sequence_num: int | None
    trade_rows: list[dict[str, Any]] = field(default_factory=list)
    level2_by_product: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def parse_coinbase_message(
    message: Mapping[str, Any],
    *,
    received_at_ns: int,
    connection_id: str,
) -> ParsedCoinbaseMessage:
    """Flatten one Coinbase message while preserving all three timestamps."""

    channel = str(message.get("channel", ""))
    sequence_num = _integer_or_none(message.get("sequence_num"))
    parsed = ParsedCoinbaseMessage(channel=channel, sequence_num=sequence_num)
    events = message.get("events", [])
    if not isinstance(events, list):
        raise CoinbaseMessageError(f"Coinbase {channel!r} events must be a list")

    if channel == "market_trades":
        for event in events:
            if not isinstance(event, Mapping):
                raise CoinbaseMessageError("Coinbase returned a malformed trade event")
            event_type = event.get("type")
            trades = event.get("trades", [])
            if not isinstance(trades, list):
                raise CoinbaseMessageError("Coinbase trade rows must be a list")
            for trade in trades:
                if not isinstance(trade, Mapping):
                    raise CoinbaseMessageError(
                        "Coinbase returned a malformed trade row"
                    )
                try:
                    row = {
                        "exchange": "coinbase",
                        "product_id": str(trade["product_id"]),
                        "trade_id": str(trade["trade_id"]),
                        "exchange_time": str(trade["time"]),
                        "received_at_ns": received_at_ns,
                        "price": str(trade["price"]),
                        "size": str(trade["size"]),
                        # Coinbase calls this field ``side``; for this channel it
                        # identifies the resting/maker side of the completed trade.
                        "maker_side": str(trade["side"]),
                        "event_type": str(event_type or ""),
                        "sequence_num": sequence_num,
                        "connection_id": connection_id,
                        "record_source": "exchange",
                    }
                except KeyError as error:
                    raise CoinbaseMessageError(
                        f"Coinbase trade is missing {error.args[0]!r}"
                    ) from error
                parsed.trade_rows.append(row)

    elif channel == "l2_data":
        message_time = str(message.get("timestamp") or "")
        for event in events:
            if not isinstance(event, Mapping):
                raise CoinbaseMessageError(
                    "Coinbase returned a malformed Level 2 event"
                )
            try:
                product_id = str(event["product_id"])
            except KeyError as error:
                raise CoinbaseMessageError(
                    "Coinbase Level 2 event is missing 'product_id'"
                ) from error
            event_type = str(event.get("type") or "")
            updates = event.get("updates", [])
            if not isinstance(updates, list):
                raise CoinbaseMessageError("Coinbase Level 2 updates must be a list")
            product_rows = parsed.level2_by_product.setdefault(product_id, [])
            for update in updates:
                if not isinstance(update, Mapping):
                    raise CoinbaseMessageError(
                        "Coinbase returned a malformed Level 2 update"
                    )
                try:
                    row = {
                        "exchange": "coinbase",
                        "product_id": product_id,
                        "event_type": event_type,
                        "event_time": str(update.get("event_time") or ""),
                        "message_time": message_time,
                        "received_at_ns": received_at_ns,
                        "side": str(update["side"]),
                        "price_level": str(update["price_level"]),
                        "new_quantity": str(update["new_quantity"]),
                        "sequence_num": sequence_num,
                        "connection_id": connection_id,
                        "record_source": "exchange",
                    }
                except KeyError as error:
                    raise CoinbaseMessageError(
                        f"Coinbase Level 2 update is missing {error.args[0]!r}"
                    ) from error
                product_rows.append(row)
    return parsed


@dataclass
class BookLevel:
    """The most recently observed state and provenance of one price level."""

    quantity: str
    event_time: str


class Level2Book:
    """Minimal full-book state used only to create rotation checkpoints."""

    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        self.levels: dict[str, dict[str, BookLevel]] = {"bid": {}, "offer": {}}
        self.has_snapshot = False
        self.last_event_time = ""
        self.last_message_time = ""
        self.last_sequence_num: int | None = None

    def reset(self) -> None:
        self.levels = {"bid": {}, "offer": {}}
        self.has_snapshot = False
        self.last_event_time = ""
        self.last_message_time = ""
        self.last_sequence_num = None

    def apply_rows(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        event_type = str(rows[0].get("event_type", ""))
        if event_type == "snapshot":
            self.reset()
            self.has_snapshot = True
        elif not self.has_snapshot:
            raise CoinbaseMessageError(
                f"received {self.product_id} Level 2 update before its snapshot"
            )

        for row in rows:
            side = str(row["side"])
            if side not in self.levels:
                raise CoinbaseMessageError(
                    f"unsupported Coinbase Level 2 side {side!r}"
                )
            price = str(row["price_level"])
            quantity = str(row["new_quantity"])
            try:
                is_zero = Decimal(quantity) == 0
                Decimal(price)
            except InvalidOperation as error:
                raise CoinbaseMessageError(
                    f"invalid Level 2 price or quantity for {self.product_id}"
                ) from error
            if is_zero:
                self.levels[side].pop(price, None)
            else:
                self.levels[side][price] = BookLevel(
                    quantity=quantity,
                    event_time=str(row.get("event_time") or ""),
                )
            self.last_event_time = str(row.get("event_time") or self.last_event_time)
            self.last_message_time = str(
                row.get("message_time") or self.last_message_time
            )
            self.last_sequence_num = _integer_or_none(row.get("sequence_num"))

    def checkpoint_rows(
        self,
        *,
        received_at_ns: int,
        connection_id: str,
    ) -> list[dict[str, Any]]:
        """Return a full, explicitly synthetic snapshot of the current book."""

        if not self.has_snapshot:
            return []
        rows: list[dict[str, Any]] = []
        for side in ("bid", "offer"):
            # Decimal sorting creates deterministic files.  Bids are written
            # best-to-worst; offers are written best-to-worst.
            reverse = side == "bid"
            sorted_levels = sorted(
                self.levels[side].items(),
                key=lambda item: Decimal(item[0]),
                reverse=reverse,
            )
            for price, level in sorted_levels:
                rows.append(
                    {
                        "exchange": "coinbase",
                        "product_id": self.product_id,
                        "event_type": "checkpoint",
                        # Preserve the level's latest exchange event time.  The
                        # local receipt timestamp identifies when the checkpoint
                        # itself was generated and observed by this process.
                        "event_time": level.event_time or self.last_event_time,
                        "message_time": self.last_message_time,
                        "received_at_ns": received_at_ns,
                        "side": side,
                        "price_level": price,
                        "new_quantity": level.quantity,
                        "sequence_num": self.last_sequence_num,
                        "connection_id": connection_id,
                        "record_source": "local_checkpoint",
                    }
                )
        return rows


class MessageSequenceTracker:
    """Detect a gap in the connection-wide Coinbase message sequence.

    Coinbase's live Advanced Trade feed increments ``sequence_num`` across the
    subscribed channels on a connection.  Consequently subscriptions, trade,
    heartbeat, and Level 2 messages must all pass through this tracker even
    though only trades and Level 2 rows are persisted.
    """

    def __init__(self) -> None:
        self.last_sequence: int | None = None

    def observe(self, sequence_num: int | None) -> None:
        if sequence_num is None:
            return
        if self.last_sequence is not None and sequence_num != self.last_sequence + 1:
            raise SequenceGapError(
                f"WebSocket sequence gap: expected {self.last_sequence + 1}, "
                f"received {sequence_num}"
            )
        self.last_sequence = sequence_num


@dataclass
class CollectorCounters:
    messages: int = 0
    trades: int = 0
    level2_rows: int = 0


async def _subscribe(socket: Any, products: Sequence[str]) -> None:
    """Subscribe one WebSocket connection to all required public channels."""

    for channel in ("market_trades", "level2"):
        await socket.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "channel": channel,
                    "product_ids": list(products),
                }
            )
        )
    # Heartbeats prevent quiet products from allowing a channel subscription to
    # close and give the health timer a regular message to observe.
    await socket.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))


async def _collect_connection(
    *,
    websocket_url: str,
    products: Sequence[str],
    output_root: Path,
    rotation_seconds: int,
    flush_seconds: float,
    stop_event: asyncio.Event,
    health_timeout_seconds: float,
) -> CollectorCounters:
    """Collect one connection lifetime; the caller handles reconnection."""

    connection_id = uuid.uuid4().hex[:12]
    books = {product: Level2Book(product) for product in products}
    sequence_tracker = MessageSequenceTracker()
    counters = CollectorCounters()
    writer = RotatingTickWriter(
        output_root,
        connection_id=connection_id,
        rotation_seconds=rotation_seconds,
        flush_seconds=flush_seconds,
    )

    logger.info(
        "Connecting to Coinbase for %s (connection_id=%s)",
        ", ".join(products),
        connection_id,
    )
    close_reason = "disconnect"
    try:
        async with connect(
            websocket_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as socket:
            await _subscribe(socket, products)
            logger.info("Coinbase subscriptions sent (connection_id=%s)", connection_id)
            last_message_monotonic = time.monotonic()
            last_status_monotonic = last_message_monotonic

            while not stop_event.is_set():
                try:
                    raw_message = await asyncio.wait_for(
                        socket.recv(), timeout=min(10.0, health_timeout_seconds)
                    )
                except asyncio.TimeoutError:
                    if (
                        time.monotonic() - last_message_monotonic
                        >= health_timeout_seconds
                    ):
                        raise ConnectionError(
                            f"no Coinbase message for {health_timeout_seconds:.0f} seconds"
                        )
                    continue

                # Capture local time immediately after receipt and before JSON
                # decoding, book maintenance, checkpointing, or disk writes.
                received_at_ns = time.time_ns()
                last_message_monotonic = time.monotonic()
                try:
                    decoded = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError) as error:
                    raise CoinbaseMessageError(
                        "Coinbase returned invalid JSON"
                    ) from error
                if not isinstance(decoded, Mapping):
                    raise CoinbaseMessageError("Coinbase message must be a JSON object")

                parsed = parse_coinbase_message(
                    decoded,
                    received_at_ns=received_at_ns,
                    connection_id=connection_id,
                )
                # Sequence validation must see every channel.  A subscription or
                # trade message can sit numerically between two Level 2 messages.
                sequence_tracker.observe(parsed.sequence_num)
                counters.messages += 1

                if parsed.channel == "l2_data":
                    for product_id, rows in parsed.level2_by_product.items():
                        if product_id not in books:
                            logger.warning(
                                "Ignoring unexpected Level 2 product %s", product_id
                            )
                            continue
                        book = books[product_id]
                        if (
                            rows
                            and rows[0].get("event_type") != "snapshot"
                            and not book.has_snapshot
                        ):
                            # Reject the event before writing it.  The next
                            # connection will begin from a complete snapshot,
                            # and no invalid update-only partition is finalized.
                            raise CoinbaseMessageError(
                                f"received {product_id} Level 2 update before "
                                "its snapshot"
                            )

                        def checkpoint_factory(
                            requested_product: str,
                            checkpoint_received_ns: int,
                        ) -> Sequence[Mapping[str, Any]]:
                            return books[requested_product].checkpoint_rows(
                                received_at_ns=checkpoint_received_ns,
                                connection_id=connection_id,
                            )

                        # The writer requests any rotation checkpoint before the
                        # new exchange update is applied to the in-memory book.
                        writer.write_level2(
                            rows,
                            checkpoint_factory=checkpoint_factory,
                        )
                        book.apply_rows(rows)
                        counters.level2_rows += len(rows)
                elif parsed.channel == "market_trades":
                    writer.write_trades(parsed.trade_rows)
                    counters.trades += len(parsed.trade_rows)

                now = time.monotonic()
                if now - last_status_monotonic >= 60:
                    logger.info(
                        "Collector healthy: %s messages, %s trades, %s Level 2 rows",
                        f"{counters.messages:,}",
                        f"{counters.trades:,}",
                        f"{counters.level2_rows:,}",
                    )
                    last_status_monotonic = now

            close_reason = "shutdown"
    finally:
        writer.close(reason="shutdown" if stop_event.is_set() else close_reason)
    return counters


async def run_collector(
    *,
    products: Sequence[str],
    output_root: Path,
    websocket_url: str = COINBASE_WS_URL,
    rotation_seconds: int = 3600,
    flush_seconds: float = 1.0,
    health_timeout_seconds: float = 45.0,
    reconnect_max_seconds: float = 60.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run until stopped, reconnecting after recoverable connection failures."""

    normalized_products = normalize_products(products)
    if health_timeout_seconds <= 0:
        raise ValueError("health_timeout_seconds must be positive")
    if reconnect_max_seconds <= 0:
        raise ValueError("reconnect_max_seconds must be positive")
    if stop_event is None:
        stop_event = asyncio.Event()

    reconnect_attempt = 0
    while not stop_event.is_set():
        try:
            await _collect_connection(
                websocket_url=websocket_url,
                products=normalized_products,
                output_root=output_root,
                rotation_seconds=rotation_seconds,
                flush_seconds=flush_seconds,
                stop_event=stop_event,
                health_timeout_seconds=health_timeout_seconds,
            )
            reconnect_attempt = 0
        except asyncio.CancelledError:
            raise
        except (
            ConnectionClosed,
            ConnectionError,
            OSError,
            CoinbaseMessageError,
        ) as error:
            if stop_event.is_set():
                break
            reconnect_attempt += 1
            base_delay = min(reconnect_max_seconds, 2 ** min(reconnect_attempt - 1, 6))
            delay = min(reconnect_max_seconds, base_delay + random.random())
            logger.exception(
                "Coinbase collection interrupted; reconnecting in %.1f seconds: %s",
                delay,
                error,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


class CollectorLock:
    """Advisory process lock that prevents two collectors writing at once."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: TextIO | None = None

    def __enter__(self) -> CollectorLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise RuntimeError(
                f"another Coinbase collector holds {self.path}"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect Coinbase trades and Level 2 updates continuously."
    )
    parser.add_argument(
        "--products",
        nargs="+",
        default=list(DEFAULT_PRODUCTS),
        help="Coinbase product IDs (default: %(default)s)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Root directory for partitioned raw files",
    )
    parser.add_argument(
        "--rotate-minutes",
        type=int,
        default=60,
        help="Finalize each product/channel file after this many minutes",
    )
    parser.add_argument(
        "--flush-seconds",
        type=float,
        default=1.0,
        help="Maximum normal interval between gzip flushes",
    )
    parser.add_argument(
        "--health-timeout-seconds",
        type=float,
        default=45.0,
        help="Reconnect when no WebSocket message arrives for this long",
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        help="Stop cleanly after this many seconds; useful for a smoke test",
    )
    parser.add_argument(
        "--websocket-url",
        default=COINBASE_WS_URL,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_LOCK_PATH,
        help=argparse.SUPPRESS,
    )
    return parser


async def _run_from_args(args: argparse.Namespace) -> None:
    if args.rotate_minutes <= 0:
        raise ValueError("--rotate-minutes must be positive")
    if args.run_seconds is not None and args.run_seconds <= 0:
        raise ValueError("--run-seconds must be positive")
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop_event.set)
        except NotImplementedError:
            # add_signal_handler isn't supported by every event loop, but the
            # Raspberry Pi/Linux service path supports it.
            pass
    if args.run_seconds is not None:
        # Use the event loop's monotonic clock so a system-clock correction does
        # not lengthen or shorten a manual smoke test.
        loop.call_later(args.run_seconds, stop_event.set)
    await run_collector(
        products=args.products,
        output_root=args.output_root,
        websocket_url=args.websocket_url,
        rotation_seconds=args.rotate_minutes * 60,
        flush_seconds=args.flush_seconds,
        health_timeout_seconds=args.health_timeout_seconds,
        stop_event=stop_event,
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        with CollectorLock(args.lock_file):
            asyncio.run(_run_from_args(args))
    except (RuntimeError, ValueError) as error:
        logger.error("Collector could not start: %s", error)
        raise SystemExit(2) from error
    except KeyboardInterrupt:
        logger.info("Collector interrupted")


if __name__ == "__main__":
    main()
