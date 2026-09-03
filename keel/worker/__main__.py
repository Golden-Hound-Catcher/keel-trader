"""
Keel worker entrypoint.

Usage:
  python -m keel.worker              # start sole scheduler (default)
  python -m keel.worker --once       # run one paper/demo trader cycle then exit
  python -m keel.worker.scheduler    # same as default (via package __main__)
"""
from __future__ import annotations

import argparse
import signal
import sys
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keel Trader worker / sole scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single paper/demo vertical cycle and exit (no scheduler loop)",
    )
    parser.add_argument(
        "--force-action",
        default="",
        help="Optional paper cycle force action (BUY_LONG / WAIT)",
    )
    args = parser.parse_args(argv)

    if args.once:
        from keel.worker.cycle import main as cycle_main

        cycle_argv = []
        if args.force_action:
            cycle_argv.extend(["--force-action", args.force_action])
        return cycle_main(cycle_argv)

    from keel.worker.scheduler import KeelScheduler

    scheduler = KeelScheduler()
    if not scheduler.start():
        print(
            "[Keel Trader] another Keel scheduler already holds the lock; exiting",
            file=sys.stderr,
        )
        return 1

    print("[Keel Trader] sole scheduler started (file lock: data/.keel_scheduler.lock)")

    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        while not stop:
            time.sleep(1.0)
    finally:
        scheduler.stop()
        print("[Keel Trader] sole scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
