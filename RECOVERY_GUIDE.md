# Keel Trader — Recovery stub

Short Keel-only recovery notes. For day-to-day ops (paper / demo / kill-switch / health), use **[RUNBOOK.md](RUNBOOK.md)**.

## What to restore

| Asset | Notes |
|-------|--------|
| `.env` | Copy from [`env.example`](env.example); fill secrets locally — never commit |
| `data/keel_ledger.db` (+ optional `-wal`/`-shm`) | SQLite ledger (`KEEL_LEDGER_DB` if overridden) |
| Repo + venv | `git clone` / pull; `pip install -e .` or project install path |

Do **not** restore legacy R20 / QwenPaw / ByPy backup trees. Those paths are retired.

## Bring-up (observe stack)

```bash
cp env.example .env   # then edit secrets on this host only
./scripts/observe_up.sh      # keel-api + keel-worker via data/run/ pidfiles
./scripts/observe_status.sh  # /health + /ready snippets (no secrets)
# optional stop:
./scripts/observe_down.sh
```

Manual alternative:

```bash
export PYTHONPATH=.
uvicorn keel.api.app:app --host 0.0.0.0 --port 8080
python -m keel.worker          # or --once
```

systemd: see `deploy/install.sh` and `deploy/README.md` (`keel-api` + `keel-worker` only).

## Smoke checks

```bash
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/ready
./scripts/ops_smoke.sh        # if present
./scripts/run_acceptance.sh   # paper acceptance
```

## Out of scope

- R20 admin / gateway DBs, QQ gateway logs, ByPy disaster packs
- Old `dashboard.app` / `r20-*` units (deleted — see [LEGACY.md](LEGACY.md))
