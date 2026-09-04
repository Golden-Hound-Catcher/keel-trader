"""
Single scheduler owner for Keel Trader.

This is THE ONLY scheduler. The legacy schedulers in r20_backend/scheduler.py
and r20_gateway/scheduler.py should be disabled.

Design principles:
- One process owns scheduling
- Jobs run in isolated subprocesses
- File-based lock prevents double-runs
"""
from __future__ import annotations

import fcntl
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

from keel.config import get_settings

BJ_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class JobSpec:
    """Specification for a scheduled job."""
    name: str
    interval_seconds: int | None = None
    schedule_times: tuple[str, ...] = ()
    timeout_seconds: int = 600


@dataclass
class JobRun:
    """Record of a job run."""
    name: str
    started_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    output: str = ""


class KeelScheduler:
    """
    Single-owner scheduler for Keel jobs.
    
    This replaces the triple-scheduler situation:
    - r20_gateway/scheduler.py (GatewayScheduler)
    - r20_backend/scheduler.py (standalone scheduler)
    - lifespan supervisor auto-spawn
    
    Only ONE scheduler should run. It uses file locks to prevent duplicates.
    """

    def __init__(
        self,
        jobs: list[JobSpec] | None = None,
        max_workers: int = 3,
    ):
        settings = get_settings()
        self._root = settings.root_dir
        self._data_dir = settings.data_dir
        self._lock_path = self._data_dir / ".keel_scheduler.lock"
        self._lock_file = None

        self._jobs = {j.name: j for j in (jobs or self._default_jobs())}
        self._last_run: dict[str, datetime] = {}
        self._runs: list[JobRun] = []
        self._running: dict[str, Future] = {}

        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="keel-job")
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _default_jobs(self) -> list[JobSpec]:
        """Default job specifications.

        SPEC-supported default is **trader only** (keel-api + keel-worker).
        Legacy R20 script jobs (factor_library / news / daily_briefing /
        nightly_backup) are opt-in via ``KEEL_ENABLE_LEGACY_SCHEDULER_JOBS=1``.

        Trader interval comes from ``settings.cycle_interval_seconds``
        (``KEEL_CYCLE_INTERVAL_SECONDS``, default 900). Timeout scales as
        ``max(840, interval - 60)`` so the classic 15min cycle keeps an 840s
        budget; longer intervals get more room, while short intervals still
        allow up to 840s for a slow run.
        """
        settings = get_settings()
        interval = settings.cycle_interval_seconds
        trader_timeout = max(840, interval - 60)
        jobs = [
            JobSpec("trader", interval_seconds=interval, timeout_seconds=trader_timeout),
        ]
        if settings.enable_legacy_scheduler_jobs:
            jobs.extend(
                [
                    JobSpec("factor_library", interval_seconds=60, timeout_seconds=55),
                    JobSpec("news", interval_seconds=10 * 60, timeout_seconds=300),
                    JobSpec(
                        "daily_briefing",
                        schedule_times=("08:00", "20:00"),
                        timeout_seconds=600,
                    ),
                    JobSpec(
                        "nightly_backup",
                        schedule_times=("02:00",),
                        timeout_seconds=1800,
                    ),
                ]
            )
        return jobs

    def _acquire_lock(self) -> bool:
        """Try to acquire the scheduler lock. Returns True if successful."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self._lock_path, "a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.seek(0)
            self._lock_file.truncate()
            self._lock_file.write(f"{time.time()}\n")
            self._lock_file.flush()
            return True
        except BlockingIOError:
            self._lock_file.close()
            self._lock_file = None
            return False

    def _release_lock(self) -> None:
        """Release the scheduler lock."""
        if self._lock_file:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            self._lock_file = None

    def _is_due(self, spec: JobSpec, now: datetime) -> bool:
        """Check if a job is due to run."""
        last = self._last_run.get(spec.name)

        if spec.interval_seconds:
            if not last:
                return True
            return (now - last).total_seconds() >= spec.interval_seconds

        if spec.schedule_times:
            current_time = now.strftime("%H:%M")
            if current_time not in spec.schedule_times:
                return False
            if last and last.date() == now.date() and last.strftime("%H:%M") == current_time:
                return False
            return True

        return False

    def _run_job(self, spec: JobSpec) -> None:
        """Run a job in a subprocess."""
        # Stage 2: trader job runs Keel paper/demo cycle (shim), not dual legacy paths.
        module_map = {
            "trader": "keel.worker.cycle",
        }
        script_map = {
            "factor_library": "scripts/factor_library.py",
            "news": "scripts/news_sentiment_harvester.py",
            "daily_briefing": "scripts/daily_summary_and_backup.py",
            "nightly_backup": "scripts/nightly_backup_and_clean.py",
        }

        run = JobRun(name=spec.name, started_at=datetime.now(BJ_TZ))

        try:
            if spec.name in module_map:
                command = [sys.executable, "-m", module_map[spec.name]]
            else:
                script = self._root / script_map.get(spec.name, f"scripts/{spec.name}.py")
                command = [sys.executable, str(script)]
            result = subprocess.run(
                command,
                cwd=self._root,
                text=True,
                capture_output=True,
                timeout=spec.timeout_seconds,
            )
            run.exit_code = result.returncode
            run.output = (result.stderr if result.returncode else result.stdout)[-2000:]
        except subprocess.TimeoutExpired:
            run.exit_code = 124
            run.output = f"Timeout after {spec.timeout_seconds}s"
        except Exception as e:
            run.exit_code = 1
            run.output = str(e)

        run.finished_at = datetime.now(BJ_TZ)
        self._runs.append(run)
        if len(self._runs) > 100:
            self._runs = self._runs[-100:]

    def _tick(self) -> list[str]:
        """Check and launch due jobs. Returns list of launched job names."""
        now = datetime.now(BJ_TZ)
        self._running = {name: f for name, f in self._running.items() if not f.done()}

        launched: list[str] = []
        for spec in self._jobs.values():
            if spec.name in self._running:
                continue
            if not self._is_due(spec, now):
                continue

            self._last_run[spec.name] = now
            future = self._executor.submit(self._run_job, spec)
            self._running[spec.name] = future
            launched.append(spec.name)

        return launched

    def _loop(self) -> None:
        """Main scheduler loop."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop_event.wait(5.0)

    def start(self) -> bool:
        """
        Start the scheduler.
        
        Returns True if started, False if another scheduler is already running.
        """
        if not self._acquire_lock():
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="keel-scheduler", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the scheduler."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._release_lock()

    def status(self) -> dict:
        """Get scheduler status."""
        now = datetime.now(BJ_TZ)
        jobs = []
        for spec in self._jobs.values():
            last = self._last_run.get(spec.name)
            jobs.append({
                "name": spec.name,
                "running": spec.name in self._running and not self._running[spec.name].done(),
                "last_run": last.isoformat() if last else None,
                "schedule": f"Every {spec.interval_seconds // 60}min" if spec.interval_seconds else ", ".join(spec.schedule_times),
            })

        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "jobs": jobs,
            "recent_runs": [
                {
                    "name": r.name,
                    "started": r.started_at.isoformat(),
                    "finished": r.finished_at.isoformat() if r.finished_at else None,
                    "exit_code": r.exit_code,
                }
                for r in self._runs[-20:]
            ],
        }


def main() -> int:
    """CLI entry for `python -m keel.worker.scheduler`."""
    import signal
    import time as _time

    scheduler = KeelScheduler()
    if not scheduler.start():
        print("Keel scheduler already running (lock held)", file=sys.stderr)
        return 1
    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        while not stop:
            _time.sleep(1.0)
    finally:
        scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
