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
    # Emergency kill switch (KEEL_KILL_SWITCH=0|1 / true|false); default off
    kill_switch: bool = False

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
        kill_switch=_env_bool("KEEL_KILL_SWITCH", False),
    )


def refresh_settings() -> Settings:
    """Clear cache and reload settings from environment."""
    get_settings.cache_clear()
    return get_settings()
