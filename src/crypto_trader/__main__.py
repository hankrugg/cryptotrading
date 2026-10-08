"""Allow the application to run with ``python -m crypto_trader``."""

from crypto_trader.main import main

# Raising SystemExit turns the integer returned by ``main`` into the process
# exit code expected by systemd and by a normal terminal invocation.
raise SystemExit(main())
