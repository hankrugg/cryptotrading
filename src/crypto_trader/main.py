"""Application entry point for the hourly signal service."""

from pathlib import Path
from dotenv import load_dotenv

from crypto_trader.logging_config import configure_logging
from crypto_trader.runner import run_forever


def main():
    # The .env file is deliberately loaded from the repository root, not from
    # the process's current directory. This makes the systemd service behave
    # the same way as a manual launch from the project directory.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    # Logging is configured before the long-running loop starts so startup,
    # data-fetch, trade, and email failures all go to the same destinations.
    configure_logging()

    # run_forever() performs one hourly evaluation immediately, then repeats.
    run_forever()


if __name__ == "__main__":
    main()
