"""Application entry point."""

from pathlib import Path
from dotenv import load_dotenv

from crypto_trader.logging_config import configure_logging
from crypto_trader.runner import run_forever


def main():
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    configure_logging()
    run_forever()


if __name__ == "__main__":
    main()
