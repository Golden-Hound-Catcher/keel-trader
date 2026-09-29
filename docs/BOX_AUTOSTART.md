# Box autostart + watchdog (reboot-proof observe stack)

**TL;DR** — after any box reboot or worker death the stack comes back by itself:

```bash
./scripts/keel_ensure_up.sh          # idempotent: venv + api/worker + watchdog (safe to run anytime)
tail -f data/run/keel-watchdog.log   # what the watchdog did and when
./scripts/install_autostart.sh       # (re)install the ~/.bashrc boot hook (idempotent)
```

## Why `.venv` vanished (9/25 23:03, 9/28, 9/29 03:53)

The box is a container that is **re-created** on reboot and hydrated from a durable snapshot
(`/tmp/sand-copy-in.log`: `outcome=hydrated … files=24063`). Snapshot-out runs every ~2 min.

| Tree | Persisted? | Notes |
|------|-----------|-------|
| `/workspace/**` | yes, **except** default ignore list: `.venv/`, `venv/`, `node_modules/`, `__pycache__/`, `*.pyc`, `.cache/`, `dist/`, `build/`, `.pytest_cache/` … (+ optional `/workspace/.sandignore`) | that is why repo `.venv/` and `frontend/node_modules/` disappear |
| `/home/box/**` | yes (whole-home category), except `.cache/`, `.npm/`, `.local/state/`, sockets … | `venv/` / `.venv/` are **not** ignored here |
| processes, `/tmp`, `/etc`, cron | **no** | PID 1 is `tini`; there is no cron/systemd in the box |

Everything else survived intact on 9/29: git HEAD/untracked files, `.env`, `data/keel_ledger.db`
(`integrity_check=ok`, last pre-reboot event 03:50:50). All mtimes read 03:53–03:54 because the
restore rewrites files. Caveat: a reboot can roll data back by up to one snapshot interval (~2 min).

## What is installed

| Piece | Where | Role |
|-------|-------|------|
| persistent venv | `/home/box/.venvs/keel-trader` (`KEEL_VENV_DIR`) | survives reboots (home is snapshotted) |
| `.venv` → symlink | repo root | the snapshot ignores the `.venv/` *directory*, not a symlink named `.venv` |
| `scripts/keel_ensure_up.sh` | repo | single-flight (`flock data/run/keel-ensure.lock`): repair venv (uv, fallback `python3 -m venv`+pip), start missing api/worker via `observe_up.sh`, restart on `/ready` unreachable or `worker_stale=true` twice in a row, start watchdog |
| `scripts/keel_watchdog.sh` | repo | one instance (`flock data/run/keel-watchdog.lock`, pid `data/run/keel-watchdog.pid`); runs `keel_ensure_up.sh --check` every `KEEL_WATCHDOG_INTERVAL` (default 120 s) |
| `~/.bashrc` hook | via `scripts/install_autostart.sh` | the first agent shell after a reboot sources `~/.bashrc` (Cursor shell snapshot is interactive) → forks `keel_ensure_up.sh --from bashrc` when the watchdog is not alive. Silent, ~5 ms when healthy |

Lock fds are closed for children (`9>&-`, `8>&-`) so api/worker never inherit them.
A dead worker's in-flight `keel.worker.cycle` child is waited for (≤180 s) before a new worker is
started, and `observe_down.sh` now does the same, so two cycles never run in parallel.

### Intentional stop is respected

`observe_down.sh` writes `data/run/keel-autostart.disabled` (persists across reboots) → the
watchdog/hook do nothing. `observe_up.sh` removes it. The watchdog's own restart uses
`observe_down.sh --keep-autostart`. One-off opt-out for a shell: `KEEL_AUTOSTART_DISABLE=1`.

Never touched: `.env`, kill switch, shadow, decision policy, orders/positions.

## Limits (be honest)

* There is no true boot hook in the box. After a reboot nothing runs until **some agent opens a
  shell** (the `~/.bashrc` hook fires then) — e.g. the scheduled heartbeat. Put
  `/workspace/keel-trader/scripts/keel_ensure_up.sh --from heartbeat` as the first step of any
  recurring automation to make this deterministic.
* Reboot survival was verified piecewise (hook fires from `bash -i`, venv symlink + home venv,
  cold venv rebuild in 2 s, watchdog restores a killed worker in ≤120 s, no duplicates under 8
  concurrent triggers), **not** by an actual reboot.
* Cold rebuild needs PyPI (uv cache under `~/.cache` is not persisted); only happens if the home
  venv is also lost.

## Verify

```bash
./scripts/observe_status.sh
cat data/run/keel-watchdog.pid && tail -20 data/run/keel-watchdog.log
kill -9 "$(cat data/run/keel-worker.pid)"   # watchdog restarts it within KEEL_WATCHDOG_INTERVAL
```
