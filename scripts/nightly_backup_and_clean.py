#!/usr/bin/env python3
"""Run one or more configured R20 custom backup jobs."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path: sys.path.insert(0, str(ROOT / "scripts"))

from r20_backend.backup_store import get_job, list_jobs
from backup_runtime import run_backup_job


def notify(result: dict) -> None:
    """QQ notifier retired; keep job hooks as local log only."""
    icon = "✅" if result["status"] == "success" else "⚠️" if result["status"] == "partial" else "❌"
    print(f"{icon} backup notify (qq retired): {result.get('job_name')} status={result.get('status')}")



def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--job-id", default=""); parser.add_argument("--all-enabled", action="store_true")
    args = parser.parse_args()
    jobs = [get_job(args.job_id)] if args.job_id else [x for x in list_jobs() if x.get("enabled")]
    if not jobs: print(json.dumps({"status": "skipped", "reason": "no enabled backup jobs"}, ensure_ascii=False)); return 0
    results = []
    for job in jobs:
        result = run_backup_job(job); results.append(result)
        if (result["status"] == "success" and job.get("notify_on_success")) or (result["status"] != "success" and job.get("notify_on_failure")): notify(result)
        print(json.dumps(result, ensure_ascii=False))
    return 0 if all(x["status"] == "success" for x in results) else 2


if __name__ == "__main__": raise SystemExit(main())
