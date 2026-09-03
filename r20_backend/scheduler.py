"""
DISABLED (Stage 2/3/7): Legacy standalone scheduler — hard quarantine.

Keel Trader uses a single scheduler owner:
    python -m keel.worker

Do NOT run this module. deploy/r20-scheduler.service cannot start
(ConditionPathExists + ExecStart=/bin/false). This file remains only as a
hard guard against accidental double-firing. See LEGACY.md.
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
