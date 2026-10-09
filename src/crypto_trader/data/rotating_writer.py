"""Durable, rotating gzip-CSV storage for Coinbase tick data.

The collector writes to files ending in ``.partial``.  A partial file is never
eligible for upload.  When a file is rotated or the collector shuts down
cleanly, the gzip stream is closed, a manifest is written, and both files are
renamed atomically to their final names.

Level 2 partitions may start with a locally generated book checkpoint.  The
checkpoint is labelled ``event_type=checkpoint`` and
``record_source=local_checkpoint`` so it cannot be mistaken for an exchange
snapshot.  It makes an hourly file independently replayable while preserving
the provenance of every row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, BinaryIO, TextIO

logger = logging.getLogger(__name__)


TRADE_FIELDS = (
    "exchange",
    "product_id",
    "trade_id",
    "exchange_time",
    "received_at_ns",
    "price",
    "size",
    "maker_side",
    "event_type",
    "sequence_num",
    "connection_id",
    "record_source",
)

LEVEL2_FIELDS = (
    "exchange",
    "product_id",
    "event_type",
    "event_time",
    "message_time",
    "received_at_ns",
    "side",
    "price_level",
    "new_quantity",
    "sequence_num",
    "connection_id",
    "record_source",
)

CHANNEL_FIELDS = {
    "trades": TRADE_FIELDS,
    "level2": LEVEL2_FIELDS,
}


def _utc_from_ns(timestamp_ns: int) -> datetime:
    """Convert a Unix nanosecond timestamp to an aware UTC datetime."""

    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc)


def _sha256(path: Path) -> str:
    """Return a streaming SHA-256 digest without loading the file into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class _OpenCsv:
    """State for one active product/channel output file."""

    product_id: str
    channel: str
    bucket_start_ns: int
    partial_path: Path
    final_path: Path
    handle: TextIO
    writer: csv.DictWriter
    opened_at_ns: int
    first_received_at_ns: int | None = None
    last_received_at_ns: int | None = None
    first_sequence_num: int | None = None
    last_sequence_num: int | None = None
    row_count: int = 0
    checkpoint_rows: int = 0
    last_flush_monotonic: float = 0.0


class RotatingTickWriter:
    """Write trade and Level 2 rows into immutable time partitions.

    A separate active file is maintained for every ``(product, channel)`` pair.
    Rotation is based on local receive time rather than exchange event time.  A
    delayed exchange event therefore remains in the file in which it was
    actually observed, preserving the bitemporal meaning of the dataset.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        connection_id: str,
        rotation_seconds: int = 3600,
        flush_seconds: float = 1.0,
        compresslevel: int = 3,
    ) -> None:
        if rotation_seconds <= 0:
            raise ValueError("rotation_seconds must be positive")
        if flush_seconds <= 0:
            raise ValueError("flush_seconds must be positive")
        if not 0 <= compresslevel <= 9:
            raise ValueError("compresslevel must be between 0 and 9")
        if not connection_id.strip():
            raise ValueError("connection_id cannot be empty")

        self.output_root = Path(output_root)
        self.connection_id = connection_id.strip()
        self.rotation_seconds = rotation_seconds
        self.rotation_ns = rotation_seconds * 1_000_000_000
        self.flush_seconds = flush_seconds
        self.compresslevel = compresslevel
        self._open: dict[tuple[str, str], _OpenCsv] = {}

    def _bucket_start_ns(self, received_at_ns: int) -> int:
        if received_at_ns < 0:
            raise ValueError("received_at_ns cannot be negative")
        return (received_at_ns // self.rotation_ns) * self.rotation_ns

    def _open_stream(
        self,
        product_id: str,
        channel: str,
        bucket_start_ns: int,
    ) -> _OpenCsv:
        fields = CHANNEL_FIELDS.get(channel)
        if fields is None:
            raise ValueError(f"unsupported channel: {channel}")

        bucket_time = _utc_from_ns(bucket_start_ns)
        directory = self.output_root / product_id / bucket_time.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = bucket_time.strftime("%Y%m%dT%H%M%SZ")
        filename = (
            f"coinbase_{product_id}_{channel}_{stamp}_{self.connection_id}.csv.gz"
        )
        final_path = directory / filename
        partial_path = directory / f"{filename}.partial"

        # Exclusive creation protects completed data if a connection identifier
        # is accidentally reused.  Files are append-only and never overwritten.
        raw_handle = partial_path.open("xb")
        gzip_handle = gzip.GzipFile(
            filename=filename,
            mode="wb",
            fileobj=raw_handle,
            compresslevel=self.compresslevel,
            mtime=bucket_start_ns // 1_000_000_000,
        )
        # newline="" is required by csv to avoid blank records on some systems.
        text_handle = TextIOWrapperWithOwner(gzip_handle, raw_handle)
        writer = csv.DictWriter(
            text_handle,
            fieldnames=fields,
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        now = time.monotonic()
        stream = _OpenCsv(
            product_id=product_id,
            channel=channel,
            bucket_start_ns=bucket_start_ns,
            partial_path=partial_path,
            final_path=final_path,
            handle=text_handle,
            writer=writer,
            opened_at_ns=time.time_ns(),
            last_flush_monotonic=now,
        )
        logger.info("Opened %s", partial_path)
        return stream

    def _stream_for(
        self,
        product_id: str,
        channel: str,
        received_at_ns: int,
    ) -> tuple[_OpenCsv, bool]:
        key = (product_id, channel)
        bucket_start_ns = self._bucket_start_ns(received_at_ns)
        stream = self._open.get(key)
        opened_new = False
        if stream is None or stream.bucket_start_ns != bucket_start_ns:
            if stream is not None:
                self._finalize(key, reason="rotation")
            stream = self._open_stream(product_id, channel, bucket_start_ns)
            self._open[key] = stream
            opened_new = True
        return stream, opened_new

    @staticmethod
    def _record_metadata(stream: _OpenCsv, row: Mapping[str, Any]) -> None:
        received_at_ns = int(row["received_at_ns"])
        sequence_value = row.get("sequence_num")
        sequence_num = None if sequence_value in (None, "") else int(sequence_value)
        if stream.first_received_at_ns is None:
            stream.first_received_at_ns = received_at_ns
            stream.first_sequence_num = sequence_num
        stream.last_received_at_ns = received_at_ns
        stream.last_sequence_num = sequence_num
        stream.row_count += 1
        if row.get("record_source") == "local_checkpoint":
            stream.checkpoint_rows += 1

    def _write_rows(self, stream: _OpenCsv, rows: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        for row in rows:
            stream.writer.writerow(row)
            self._record_metadata(stream, row)
            count += 1
        if time.monotonic() - stream.last_flush_monotonic >= self.flush_seconds:
            stream.handle.flush()
            stream.last_flush_monotonic = time.monotonic()
        return count

    def write_trades(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Write one parsed Coinbase trade batch."""

        if not rows:
            return
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["product_id"]), []).append(row)
        for product_id, product_rows in grouped.items():
            stream, _ = self._stream_for(
                product_id,
                "trades",
                int(product_rows[0]["received_at_ns"]),
            )
            self._write_rows(stream, product_rows)

    def write_level2(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        checkpoint_factory: Callable[[str, int], Sequence[Mapping[str, Any]]],
    ) -> None:
        """Write Level 2 updates, adding a checkpoint after time rotation.

        ``checkpoint_factory`` is called only when a new time bucket begins and
        its first exchange event isn't already a snapshot.  The factory receives
        the product and the current message's local receive timestamp.
        """

        if not rows:
            return
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["product_id"]), []).append(row)

        for product_id, product_rows in grouped.items():
            received_at_ns = int(product_rows[0]["received_at_ns"])
            stream, opened_new = self._stream_for(
                product_id,
                "level2",
                received_at_ns,
            )
            first_event_type = str(product_rows[0].get("event_type", ""))
            if opened_new and first_event_type != "snapshot":
                checkpoint_rows = checkpoint_factory(product_id, received_at_ns)
                self._write_rows(stream, checkpoint_rows)
            self._write_rows(stream, product_rows)

    def _finalize(self, key: tuple[str, str], *, reason: str) -> None:
        stream = self._open.pop(key)
        # Closing TextIOWrapper cascades through GzipFile and its owned raw file.
        stream.handle.flush()
        stream.handle.close()
        os.replace(stream.partial_path, stream.final_path)

        closed_at_ns = time.time_ns()
        manifest = {
            "schema_version": 1,
            "exchange": "coinbase",
            "product_id": stream.product_id,
            "channel": stream.channel,
            "connection_id": self.connection_id,
            "rotation_seconds": self.rotation_seconds,
            "bucket_start": _utc_from_ns(stream.bucket_start_ns).isoformat(),
            "opened_at": _utc_from_ns(stream.opened_at_ns).isoformat(),
            "closed_at": _utc_from_ns(closed_at_ns).isoformat(),
            "first_received_at_ns": stream.first_received_at_ns,
            "last_received_at_ns": stream.last_received_at_ns,
            "first_sequence_num": stream.first_sequence_num,
            "last_sequence_num": stream.last_sequence_num,
            "row_count": stream.row_count,
            "checkpoint_rows": stream.checkpoint_rows,
            "close_reason": reason,
            "filename": stream.final_path.name,
            "compressed_bytes": stream.final_path.stat().st_size,
            "sha256": _sha256(stream.final_path),
        }
        manifest_path = Path(f"{stream.final_path}.manifest.json")
        manifest_partial = Path(f"{manifest_path}.partial")
        with manifest_partial.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(manifest_partial, manifest_path)
        logger.info(
            "Finalized %s (%s rows, %s checkpoint rows, reason=%s)",
            stream.final_path,
            f"{stream.row_count:,}",
            f"{stream.checkpoint_rows:,}",
            reason,
        )

    def close(self, *, reason: str = "shutdown") -> None:
        """Finalize all active files."""

        for key in list(self._open):
            self._finalize(key, reason=reason)

    def __enter__(self) -> RotatingTickWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(reason="error" if exc_type is not None else "shutdown")


class TextIOWrapperWithOwner:
    """Small text wrapper that closes both gzip and its raw file.

    ``gzip.GzipFile`` deliberately leaves a caller-provided ``fileobj`` open.
    This wrapper makes ownership explicit so an hourly rotation cannot leak a
    raw file descriptor on a long-running Raspberry Pi.
    """

    def __init__(self, gzip_handle: gzip.GzipFile, raw_handle: BinaryIO) -> None:
        import io

        self._gzip_handle = gzip_handle
        self._raw_handle = raw_handle
        self._text = io.TextIOWrapper(gzip_handle, encoding="utf-8", newline="")

    def write(self, value: str) -> int:
        return self._text.write(value)

    def flush(self) -> None:
        self._text.flush()

    def close(self) -> None:
        if self._text.closed:
            return
        try:
            self._text.close()
        finally:
            # TextIOWrapper closes the gzip layer, while GzipFile intentionally
            # leaves a caller-owned raw handle open.  Sync the finalized gzip
            # bytes before closing and atomically renaming the file.
            self._raw_handle.flush()
            os.fsync(self._raw_handle.fileno())
            self._raw_handle.close()

    @property
    def closed(self) -> bool:
        return self._text.closed
