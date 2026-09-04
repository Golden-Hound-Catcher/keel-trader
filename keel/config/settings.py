"""
Centralized configuration for Keel Trader.

This consolidates the credential sprawl from:
- .env files
- encrypted stores
- ~/.okx
- llm_models.json keys

Strategy: environment variables for secrets, non-secret flags in config.
Single settings path for OKX demo/live + LLM (Stage 5). Prefer KEEL_* names.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Settings:
    """Immutable configuration container."""

    # Paths
    root_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "data")
    logs_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "logs")

    # OKX Exchange
    okx_environment: Literal["demo", "live"] = "demo"
    okx_api_key: str = ""
    okx_secret_key: str = ""
    okx_passphrase: str = ""

    # LLM
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_reasoning_effort: str = "high"

    # API Server
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    # Optional shared bearer for non-local binds (empty → no auth)
    api_token: str = ""

    # Ledger (SQLite). Override with KEEL_LEDGER_DB for tests / alternate data dirs.
    ledger_db: str = ""

    # Optional notifications (empty → NullNotifier; see keel.notify)
    notify_webhook_url: str = ""

    # Risk Limits
    max_concurrent_positions: int = 6
    max_same_direction_positions: int = 6
    max_daily_loss_usdt: float = 150.0
    max_single_asset_margin: float = 600.0
    # Max order+position notional per instrument (USDT). Default 2000 ≈ margin 600 × ~3.3 lev.
    max_notional_per_instrument: float = 2000.0
    # Max contracts (size units) per instrument; used when GateContext.size > 0.
    max_contracts_per_instrument: int = 50
    # Emergency kill switch (KEEL_KILL_SWITCH=0|1 / true|false); default off
    kill_switch: bool = False

    # Trader cycle interval (KEEL_CYCLE_INTERVAL_SECONDS); default 900 = 15min
    cycle_interval_seconds: int = 900

    # Opt-in legacy script jobs in KeelScheduler (KEEL_ENABLE_LEGACY_SCHEDULER_JOBS)
    enable_legacy_scheduler_jobs: bool = False

    # Active OKX swap instruments (KEEL_INSTRUMENTS); empty env → DEFAULT_CRYPTO_INSTRUMENTS
    instruments: tuple[str, ...] = ()

    @property
    def is_demo(self) -> bool:
        return self.okx_environment == "demo"

    @property
    def okx_configured(self) -> bool:
        return bool(self.okx_api_key and self.okx_secret_key and self.okx_passphrase)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def notify_configured(self) -> bool:
        return bool(self.notify_webhook_url.strip())

    @property
    def exchange_mode(self) -> str:
        """Worker adapter selection: okx_rest when keys exist, else paper."""
        if self.okx_configured:
            return f"okx_rest:{self.okx_environment}"
        return "paper"

    @property
    def scheduler_jobs(self) -> tuple[str, ...]:
        """Job names KeelScheduler would run with current settings (trader-only by default)."""
        jobs: tuple[str, ...] = ("trader",)
        if self.enable_legacy_scheduler_jobs:
            jobs = jobs + (
                "factor_library",
                "news",
                "daily_briefing",
                "nightly_backup",
            )
        return jobs

    @property
    def ledger_path(self) -> Path:
        if self.ledger_db:
            return Path(self.ledger_db)
        return self.data_dir / "keel_ledger.db"


def _env(key: str, default: str = "") -> str:
    """Read from environment with fallback."""
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    """Read boolean from environment."""
    val = _env(key, "").lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    """Read integer from environment."""
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    """Read float from environment."""
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


# Trader cycle interval bounds (seconds): min 1m, max 24h.
CYCLE_INTERVAL_MIN_SECONDS = 60
CYCLE_INTERVAL_MAX_SECONDS = 86400
CYCLE_INTERVAL_DEFAULT_SECONDS = 900


def clamp_cycle_interval_seconds(value: int) -> int:
    """Clamp trader cycle interval to [60, 86400]."""
    return max(CYCLE_INTERVAL_MIN_SECONDS, min(CYCLE_INTERVAL_MAX_SECONDS, int(value)))


def _env_cycle_interval_seconds() -> int:
    """Parse KEEL_CYCLE_INTERVAL_SECONDS with default 900 and sane clamp."""
    raw = _env_int("KEEL_CYCLE_INTERVAL_SECONDS", CYCLE_INTERVAL_DEFAULT_SECONDS)
    return clamp_cycle_interval_seconds(raw)


def parse_instruments(raw: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """
    Parse instrument ids: strip, drop empties, dedupe (preserve order).

    Empty input → DEFAULT_CRYPTO_INSTRUMENTS ids.
    """
    from keel.domain.instruments import DEFAULT_CRYPTO_INSTRUMENTS

    if raw is None:
        parts: list[str] = []
    elif isinstance(raw, str):
        parts = raw.split(",")
    else:
        parts = list(raw)

    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        inst_id = str(part).strip()
        if not inst_id or inst_id in seen:
            continue
        seen.add(inst_id)
        out.append(inst_id)
    if not out:
        return tuple(i.inst_id for i in DEFAULT_CRYPTO_INSTRUMENTS)
    return tuple(out)


def _env_instruments() -> tuple[str, ...]:
    """Parse KEEL_INSTRUMENTS (comma-separated OKX swap ids)."""
    return parse_instruments(_env("KEEL_INSTRUMENTS", ""))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings from environment. Cached for performance."""
    okx_env = (_env("KEEL_OKX_ENV") or _env("R20_OKX_ENV", "demo")).lower()
    if okx_env not in ("demo", "live"):
        okx_env = "demo"

    # Prefer KEEL_* then env-specific OKX_* then legacy single-key names.
    okx_api_key = (
        _env("KEEL_OKX_API_KEY")
        or _env(f"OKX_{okx_env.upper()}_API_KEY")
        or _env("OKX_API_KEY")
    )
    okx_secret = (
        _env("KEEL_OKX_SECRET_KEY")
        or _env(f"OKX_{okx_env.upper()}_SECRET_KEY")
        or _env("OKX_SECRET_KEY")
    )
    okx_pass = (
        _env("KEEL_OKX_PASSPHRASE")
        or _env(f"OKX_{okx_env.upper()}_PASSPHRASE")
        or _env("OKX_PASSPHRASE")
    )

    return Settings(
        okx_environment=okx_env,  # type: ignore[arg-type]
        okx_api_key=okx_api_key,
        okx_secret_key=okx_secret,
        okx_passphrase=okx_pass,
        llm_base_url=_env("KEEL_LLM_BASE_URL") or _env("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=_env("KEEL_LLM_API_KEY") or _env("LLM_API_KEY") or _env("OPENAI_API_KEY"),
        llm_model=_env("KEEL_LLM_MODEL") or _env("LLM_MODEL", "gpt-4o"),
        llm_reasoning_effort=_env("KEEL_LLM_REASONING_EFFORT") or _env("LLM_REASONING_EFFORT", "high"),
        api_host=_env("KEEL_API_HOST", "0.0.0.0"),
        api_port=_env_int("KEEL_API_PORT", 8080),
        api_token=_env("KEEL_API_TOKEN", ""),
        ledger_db=_env("KEEL_LEDGER_DB", ""),
        notify_webhook_url=_env("KEEL_NOTIFY_WEBHOOK_URL", ""),
        max_concurrent_positions=_env_int("KEEL_MAX_POSITIONS", 6),
        max_daily_loss_usdt=_env_float("KEEL_MAX_DAILY_LOSS", 150.0),
        max_single_asset_margin=_env_float("KEEL_MAX_ASSET_MARGIN", 600.0),
        max_notional_per_instrument=_env_float("KEEL_MAX_NOTIONAL_PER_INSTRUMENT", 2000.0),
        max_contracts_per_instrument=_env_int("KEEL_MAX_CONTRACTS_PER_INSTRUMENT", 50),
        kill_switch=_env_bool("KEEL_KILL_SWITCH", False),
        cycle_interval_seconds=_env_cycle_interval_seconds(),
        enable_legacy_scheduler_jobs=_env_bool("KEEL_ENABLE_LEGACY_SCHEDULER_JOBS", False),
        instruments=_env_instruments(),
    )


def refresh_settings() -> Settings:
    """Clear cache and reload settings from environment."""
    get_settings.cache_clear()
    return get_settings()
