"""Command-line entry point.

Startup order is the point of this module:

    parse config -> construct the complete runtime config -> validate every
    downstream invariant -> only then connect anything

Nothing is created, connected, or started before the configuration is known to
be usable. An invalid setting therefore costs an error message and a non-zero
exit, never a half-started process holding a broker connection.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import time
from zoneinfo import ZoneInfo

from .broker import IBKRBroker
from .clock import Clock, SystemClock
from .config import RunConfig, load_config
from .errors import BrokerNotConnected, ConfigError
from .reviewer import ClaudeReviewer
from .runner import Runner
from .scanner import IBKRMarketData
from .store import SqliteStore

log = logging.getLogger("ibkr_trader")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_BROKER_ERROR = 3

_EASTERN = ZoneInfo("America/New_York")
_OPEN = time(9, 30)
_CLOSE = time(16, 0)


def is_market_open(clock: Clock) -> bool:
    """Whether the US equity market is in its regular session.

    Weekday and session-hours only.

    Note:
        This does **not** know about market holidays or early closes. On a
        holiday it reports open, and the scan finds no quotes and records data
        errors rather than trading. That is a deliberate trade-off against
        taking on a holiday-calendar dependency; if unattended holiday runs
        matter, supply a real calendar here.
    """
    now = clock.now().astimezone(_EASTERN)
    if now.weekday() >= 5:
        return False
    return _OPEN <= now.time() <= _CLOSE


def configure_logging(verbose: bool) -> None:
    """Send operator output to stdout, one line per symbol.

    The format is bare so the per-symbol outcome lines read as a table rather
    than as log records.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def build_runner(config: RunConfig, clock: Clock) -> tuple[Runner, IBKRBroker]:
    """Wire the production runner.

    One ``IB`` client is shared by the scanner and the broker: they are two
    views of the same session, and opening a second connection would consume a
    second client id for no reason.
    """
    broker = IBKRBroker(config.ibkr, clock)
    broker.connect()

    market_data = IBKRMarketData(
        ibkr_config=config.ibkr,
        strategy_config=config.strategy,
        clock=clock,
        ib=broker.client,
    )
    runner = Runner(
        config=config,
        market_data=market_data,
        reviewer=ClaudeReviewer(config.reviewer, clock),
        broker=broker,
        store=SqliteStore(config.database_path, clock=clock),
        clock=clock,
    )
    return runner, broker


def main(argv: list[str] | None = None) -> int:
    """Run one pass, or repeat until the close."""
    parser = argparse.ArgumentParser(prog="ibkr_trader", description=__doc__)
    parser.add_argument(
        "command",
        choices=("run", "loop"),
        help="'run' scans the universe once; 'loop' repeats until the close",
    )
    parser.add_argument(
        "-c", "--config", default="trader.toml", help="path to the TOML config file"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    # Configuration first. Written to stderr rather than logged because this is
    # a startup contract violation, not a runtime event: it must be visible even
    # if logging is redirected, and it must precede any machinery starting.
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    clock = SystemClock()
    try:
        runner, broker = build_runner(config, clock)
    except BrokerNotConnected as exc:
        print(f"Cannot connect to IBKR: {exc}", file=sys.stderr)
        return EXIT_BROKER_ERROR

    try:
        if args.command == "run":
            runner.run_once()
        else:
            runner.run_while(lambda: is_market_open(clock))
    finally:
        broker.disconnect()

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
