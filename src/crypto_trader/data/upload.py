"""Upload finalized tick partitions to remote storage with rclone.

The uploader is deliberately separate from the WebSocket collector.  A Google
Drive outage can delay backups, but it cannot stop local market-data capture.
Only finalized ``.csv.gz`` files and their manifests are eligible; active
``.partial`` files are excluded.  Move mode removes each eligible local file
only after rclone has successfully copied and checked it at the destination.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shutil
import subprocess
from collections.abc import Sequence

from crypto_trader.data.coinbase_ticks import DEFAULT_OUTPUT_ROOT
from crypto_trader.logging_config import configure_logging

logger = logging.getLogger(__name__)


def build_rclone_command(
    *,
    source: Path,
    remote: str,
    rclone_binary: str = "rclone",
    move: bool = False,
    minimum_age_minutes: int = 2,
) -> list[str]:
    """Build a conservative rclone command for immutable collector files."""

    if not remote.strip() or ":" not in remote:
        raise ValueError("remote must look like 'gdrive:folder/path'")
    if minimum_age_minutes < 0:
        raise ValueError("minimum_age_minutes cannot be negative")
    operation = "move" if move else "copy"
    command = [
        rclone_binary,
        operation,
        str(source),
        remote,
        # Completed filenames are unique and immutable.  Refusing to overwrite
        # a different remote object is safer than silently replacing data.
        "--immutable",
        "--include",
        "**/*.csv.gz",
        "--include",
        "**/*.csv.gz.manifest.json",
        "--exclude",
        "*",
        "--checkers",
        "4",
        "--transfers",
        "2",
        "--retries",
        "5",
        "--low-level-retries",
        "10",
        "--log-level",
        "INFO",
    ]
    if minimum_age_minutes:
        command.extend(["--min-age", f"{minimum_age_minutes}m"])
    if move:
        # The data files are removed by rclone only after successful transfer.
        # Removing directories is safe because rclone can delete only folders
        # that are actually empty; a directory containing an active .partial
        # file remains in place.
        command.append("--delete-empty-src-dirs")
    return command


def upload_completed_files(
    *,
    source: Path,
    remote: str,
    rclone_binary: str = "rclone",
    move: bool = False,
    minimum_age_minutes: int = 2,
) -> None:
    """Copy or move finalized files, raising if rclone reports a failure."""

    source = Path(source)
    source.mkdir(parents=True, exist_ok=True)
    resolved_binary = shutil.which(rclone_binary)
    if resolved_binary is None:
        raise RuntimeError(
            f"{rclone_binary!r} was not found; install and configure rclone first"
        )
    command = build_rclone_command(
        source=source,
        remote=remote,
        rclone_binary=resolved_binary,
        move=move,
        minimum_age_minutes=minimum_age_minutes,
    )
    action = "Moving" if move else "Copying"
    logger.info("%s finalized tick files from %s to %s", action, source, remote)
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"rclone failed with exit code {completed.returncode}")
    logger.info("Tick-data upload completed successfully")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload completed Coinbase tick files with rclone."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Local Coinbase raw-data root",
    )
    parser.add_argument(
        "--remote",
        default=os.getenv("TICK_UPLOAD_REMOTE"),
        required=os.getenv("TICK_UPLOAD_REMOTE") is None,
        help="rclone destination, for example gdrive:coinbase-data/raw",
    )
    parser.add_argument(
        "--rclone-binary",
        default="rclone",
        help="rclone executable name or path",
    )
    parser.add_argument(
        "--minimum-age-minutes",
        type=int,
        default=2,
        help="Ignore very recently finalized files",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help=(
            "remove local completed files after rclone successfully transfers "
            "and checks them; active and failed files remain local"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    configure_logging()
    try:
        upload_completed_files(
            source=args.source,
            remote=args.remote,
            rclone_binary=args.rclone_binary,
            move=args.move,
            minimum_age_minutes=args.minimum_age_minutes,
        )
    except (RuntimeError, ValueError) as error:
        logger.error("Upload failed: %s", error)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
