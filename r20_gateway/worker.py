"""Single-owner R20 Gateway delivery worker."""
from __future__ import annotations
import fcntl
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from r20_gateway.channels import NotificationChannelAdapter
from r20_gateway.publisher import DB_PATH
from r20_gateway.scheduler import GatewayScheduler
from r20_gateway.store import GatewayStore

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "data" / ".r20_gateway.lock"
LOG_FILE = ROOT / "logs" / "r20_gateway.log"
BJ_TZ = timezone(timedelta(hours=8))
RUNNING = True


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def stop(*_: object) -> None:
    global RUNNING
    RUNNING = False


def format_message(row: dict[str, object]) -> str:
    return f"【Keel Trader】{row['created_at']}\n{row['title']}\n{str(row['message']).strip()}"


def run() -> None:
    """Notification delivery only. Job scheduling owned by keel.worker (Stage 2)."""
    import os
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("gateway worker already running; exiting")
        return
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store = GatewayStore(DB_PATH)
    store.recover_processing()
    # Stage 2: GatewayScheduler job ticks are disabled to prevent double-firing trader.
    # Set KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1 only for emergency rollback.
    enable_legacy = os.environ.get("KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER", "").strip() == "1"
    scheduler = None
    if enable_legacy:
        scheduler = GatewayScheduler(store)
        scheduler.initialize_migration_baseline()
        log("WARNING: legacy GatewayScheduler ENABLED (KEEL_ENABLE_LEGACY_GATEWAY_SCHEDULER=1)")
    else:
        log("gateway worker started as notification delivery only (Keel owns scheduling)")
    while RUNNING:
        if scheduler is not None:
            launched = scheduler.tick()
            for job_name in launched:
                log(f"scheduled job={job_name}")
        deliveries = store.claim_due(20)
        if not deliveries:
            time.sleep(1)
            continue
        for delivery in deliveries:
            try:
                result = NotificationChannelAdapter(str(delivery["channel"])).send(format_message(delivery))
                if result.success:
                    store.complete(int(delivery["id"]), result.status, result.detail)
                    log(f"{result.status} event={delivery['event_id']} channel={delivery['channel']} detail={result.detail}")
                else:
                    store.fail(int(delivery["id"]), int(delivery["attempts"]), result.detail)
                    log(f"delivery failed event={delivery['event_id']} channel={delivery['channel']} detail={result.detail}")
            except Exception as exc:
                store.fail(int(delivery["id"]), int(delivery["attempts"]), str(exc))
                log(f"delivery exception event={delivery['event_id']} channel={delivery['channel']} type={type(exc).__name__}")
    if scheduler is not None:
        scheduler.shutdown()
    log("gateway worker stopped")


if __name__ == "__main__":
    run()
