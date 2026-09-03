"""
DISABLED (Stage 2): Legacy standalone scheduler.

Keel Trader uses a single scheduler owner:
    python -m keel.worker

Do NOT run this module. deploy/r20-scheduler.service is also disabled.
This file remains only as a hard guard against accidental double-firing.
"""
from __future__ import annotations
import sys


def main() -> None:
    print(
        "DISABLED: r20_backend.scheduler is retired.\n"
        "Use the sole Keel scheduler instead:\n"
        "  python -m keel.worker\n"
        "  python -m keel.worker --once   # one paper/demo cycle\n",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    main()
