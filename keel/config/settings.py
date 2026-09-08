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

_DOTENV_LOADED = False
_DOTENV_VALUES: dict[str, str] = {}



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
    # When true, skip webhook unless cycle payload alert=True
    notify_alerts_only: bool = False
    # Webhook JSON shape: keel={"event","payload"} | discord={"content": text}
    notify_format: Literal["keel", "discord"] = "keel"

    # Risk Limits
    max_concurrent_positions: int = 6
    max_same_direction_positions: int = 6
    max_daily_loss_usdt: float = 150.0
    max_single_asset_margin: float = 600.0
    # Max order+position notional per instrument (USDT). Default 2000 ≈ margin 600 × ~3.3 lev.
    max_notional_per_instrument: float = 2000.0
    # Max contracts (size units) per instrument; used when GateContext.size > 0.
    max_contracts_per_instrument: int = 50
    # First-live tighter caps (KEEL_LIVE_MAX_*); used when env=live AND not shadow_mode.
    live_max_notional_per_instrument: float = 200.0
    live_max_contracts_per_instrument: int = 5
    # Emergency kill switch (KEEL_KILL_SWITCH=0|1 / true|false); default off
    kill_switch: bool = False
    # Shadow execution (KEEL_SHADOW_MODE=0|1); when on, ledger shadow fills instead of place_order
    shadow_mode: bool = False
    # Arming: require recent shadow_fill rehearsal (hours / hard require).
    arming_shadow_hours: float = 24.0
    arming_require_shadow: bool = False
    # S1 economic arming gates (read-only; kill still cleared manually).
    arming_econ_enabled: bool = True
    arming_econ_hours: float = 24.0
    arming_econ_min_fills: int = 10
    arming_econ_min_probe_fills: int = 5
    arming_econ_min_markout_sample: int = 5
    arming_econ_markout_horizon_seconds: int = 300
    arming_econ_min_probe_win_rate_net_rt: float = 0.55
    arming_econ_min_avg_net_rt_bps: float = 0.0
    # Q3: convert strong WAIT near-signals into shadow fills (kill+shadow only).
    shadow_near_probe: bool = False
    shadow_near_probe_cooldown_seconds: int = 900
    shadow_near_probe_max_missing: int = 2
    shadow_near_probe_min_confidence: float = 0.0
    # Q3.4: fee-aware min edge hurdle for near-probe (None → use RT/open fee bps).
    shadow_near_probe_min_edge_bps: float | None = None
    shadow_near_probe_edge_mode: str = "round_trip"  # round_trip|open
    # E3: per-instrument full-gate rule fire cooldown (0 disables; clamp 0–7200).
    rule_fire_cooldown_seconds: int = 900
    # Q3.3 fee-aware shadow markout (OKX USDT-SWAP makerU/takerU).
    # Role default taker — shadow/near_probe assume immediate fill.
    shadow_fee_role: str = "taker"  # taker|maker
    # Optional bps overrides (positive=cost, negative=rebate); None → live/fallback.
    shadow_maker_fee_bps: float | None = None
    shadow_taker_fee_bps: float | None = None

    # Trader cycle interval (KEEL_CYCLE_INTERVAL_SECONDS / KEEL_OBSERVE_PRESET); default 900
    cycle_interval_seconds: int = 900
    # Observation cadence preset name (default|fast|slow); None when unset
    observe_preset: str | None = None

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
    def uses_live_caps(self) -> bool:
        """True when real live path should use tighter KEEL_LIVE_MAX_* caps."""
        return self.okx_environment == "live" and not self.shadow_mode

    @property
    def effective_max_notional_per_instrument(self) -> float:
        """Notional cap in force: live tighter caps on real live; else KEEL_MAX_*."""
        if self.uses_live_caps:
            return self.live_max_notional_per_instrument
        return self.max_notional_per_instrument

    @property
    def effective_max_contracts_per_instrument(self) -> int:
        """Contracts cap in force: live tighter caps on real live; else KEEL_MAX_*."""
        if self.uses_live_caps:
            return self.live_max_contracts_per_instrument
        return self.max_contracts_per_instrument

    @property
    def exchange_mode(self) -> str:
        """Worker adapter selection: okx_rest when keys exist, else paper."""
        if self.okx_configured:
            return f"okx_rest:{self.okx_environment}"
        return "paper"

    @property
    def scheduler_jobs(self) -> tuple[str, ...]:
        """Job names KeelScheduler runs (trader only)."""
        return ("trader",)

    @property
    def ledger_path(self) -> Path:
        if self.ledger_db:
            return Path(self.ledger_db)
        return self.data_dir / "keel_ledger.db"


def _env(key: str, default: str = "") -> str:
    """Read process env first, then repo ``.env`` map, then default."""
    if key in os.environ:
        return os.environ[key]
    if key in _DOTENV_VALUES:
        return _DOTENV_VALUES[key]
    return default


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


def _env_optional_float(key: str) -> float | None:
    """Read optional float; empty/unset → None."""
    raw = _env(key, "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_shadow_fee_role() -> str:
    """taker (default) | maker for shadow markout fee legs."""
    raw = (_env("KEEL_SHADOW_FEE_ROLE", "taker") or "taker").strip().lower()
    return "maker" if raw == "maker" else "taker"


def _env_shadow_near_probe_edge_mode() -> str:
    """round_trip (default) | open — which fee leg is the near-probe hurdle."""
    raw = (_env("KEEL_SHADOW_NEAR_PROBE_EDGE_MODE", "round_trip") or "round_trip").strip().lower()
    return "open" if raw == "open" else "round_trip"


# Trader cycle interval bounds (seconds): min 1m, max 24h.
CYCLE_INTERVAL_MIN_SECONDS = 60
CYCLE_INTERVAL_MAX_SECONDS = 86400
CYCLE_INTERVAL_DEFAULT_SECONDS = 900

# Q0 observation cadence presets (KEEL_OBSERVE_PRESET). Explicit seconds still win.
OBSERVE_PRESET_SECONDS: dict[str, int] = {
    "default": 900,
    "fast": 300,
    "slow": 1800,
}


def clamp_rule_fire_cooldown_seconds(value: int) -> int:
    """Clamp KEEL_RULE_FIRE_COOLDOWN_SECONDS into [0, 7200]; 0 disables (E3)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 900
    return max(0, min(7200, v))


def clamp_cycle_interval_seconds(value: int) -> int:
    """Clamp trader cycle interval to [60, 86400]."""
    return max(CYCLE_INTERVAL_MIN_SECONDS, min(CYCLE_INTERVAL_MAX_SECONDS, int(value)))


def _env_key_present(key: str) -> bool:
    """True when key is set in process env or loaded .env map (even if empty)."""
    return key in os.environ or key in _DOTENV_VALUES


def resolve_observe_preset(raw: str | None = None) -> str | None:
    """Normalize KEEL_OBSERVE_PRESET to default|fast|slow, else None."""
    if raw is None:
        raw = _env("KEEL_OBSERVE_PRESET", "")
    name = (raw or "").strip().lower()
    if name in OBSERVE_PRESET_SECONDS:
        return name
    return None


def resolve_cycle_interval_seconds(
    *,
    explicit_seconds: str | None = None,
    preset: str | None = None,
    explicit_set: bool | None = None,
) -> tuple[int, str | None]:
    """
    Resolve effective cycle interval + observe preset name.

    Priority: explicit ``KEEL_CYCLE_INTERVAL_SECONDS`` (if set) > ``KEEL_OBSERVE_PRESET``
    mapping > default 900. Returns ``(clamped_seconds, preset_or_none)``.
    When seconds are explicit, the preset name is still returned if valid (for /config).
    """
    preset_name = resolve_observe_preset(preset)

    if explicit_set is None:
        explicit_set = _env_key_present("KEEL_CYCLE_INTERVAL_SECONDS")
    if explicit_seconds is None and explicit_set:
        explicit_seconds = _env("KEEL_CYCLE_INTERVAL_SECONDS", "")

    if explicit_set:
        try:
            value = int(str(explicit_seconds).strip()) if str(explicit_seconds or "").strip() else CYCLE_INTERVAL_DEFAULT_SECONDS
        except ValueError:
            value = CYCLE_INTERVAL_DEFAULT_SECONDS
        return clamp_cycle_interval_seconds(value), preset_name

    if preset_name is not None:
        return clamp_cycle_interval_seconds(OBSERVE_PRESET_SECONDS[preset_name]), preset_name

    return CYCLE_INTERVAL_DEFAULT_SECONDS, None


def _env_cycle_interval_seconds() -> tuple[int, str | None]:
    """Parse cycle interval + observe preset from env / .env."""
    return resolve_cycle_interval_seconds()


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



def _env_notify_format() -> Literal["keel", "discord"]:
    """Parse KEEL_NOTIFY_FORMAT; unknown values fall back to keel."""
    raw = (_env("KEEL_NOTIFY_FORMAT", "keel") or "keel").strip().lower()
    if raw == "discord":
        return "discord"
    return "keel"


def _load_dotenv_files() -> None:
    """
    Load repo-root ``.env`` into an in-memory map (not ``os.environ``).

    Process env always wins via ``_env``. Set ``KEEL_SKIP_DOTENV=1`` to skip
    (pytest does this so a developer ``.env`` cannot leak into tests).
    Idempotent.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    _DOTENV_VALUES.clear()
    skip = (os.environ.get("KEEL_SKIP_DOTENV", "") or "").lower()
    if skip in ("1", "true", "yes"):
        return
    root = Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    if not env_path.is_file():
        return
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        _DOTENV_VALUES[key] = value.strip().strip('"').strip("'")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings from environment (and repo ``.env``). Cached for performance."""
    _load_dotenv_files()
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

    cycle_interval_seconds, observe_preset = _env_cycle_interval_seconds()

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
        notify_alerts_only=_env_bool("KEEL_NOTIFY_ALERTS_ONLY", False),
        notify_format=_env_notify_format(),
        max_concurrent_positions=_env_int("KEEL_MAX_POSITIONS", 6),
        max_daily_loss_usdt=_env_float("KEEL_MAX_DAILY_LOSS", 150.0),
        max_single_asset_margin=_env_float("KEEL_MAX_ASSET_MARGIN", 600.0),
        max_notional_per_instrument=_env_float("KEEL_MAX_NOTIONAL_PER_INSTRUMENT", 2000.0),
        max_contracts_per_instrument=_env_int("KEEL_MAX_CONTRACTS_PER_INSTRUMENT", 50),
        live_max_notional_per_instrument=_env_float("KEEL_LIVE_MAX_NOTIONAL_PER_INSTRUMENT", 200.0),
        live_max_contracts_per_instrument=_env_int("KEEL_LIVE_MAX_CONTRACTS_PER_INSTRUMENT", 5),
        kill_switch=_env_bool("KEEL_KILL_SWITCH", False),
        shadow_mode=_env_bool("KEEL_SHADOW_MODE", False),
        arming_shadow_hours=_env_float("KEEL_ARMING_SHADOW_HOURS", 24.0),
        arming_require_shadow=_env_bool("KEEL_ARMING_REQUIRE_SHADOW", False),
        arming_econ_enabled=_env_bool("KEEL_ARMING_ECON_ENABLED", True),
        arming_econ_hours=_env_float("KEEL_ARMING_ECON_HOURS", 24.0),
        arming_econ_min_fills=_env_int("KEEL_ARMING_ECON_MIN_FILLS", 10),
        arming_econ_min_probe_fills=_env_int("KEEL_ARMING_ECON_MIN_PROBE_FILLS", 5),
        arming_econ_min_markout_sample=_env_int("KEEL_ARMING_ECON_MIN_MARKOUT_SAMPLE", 5),
        arming_econ_markout_horizon_seconds=_env_int("KEEL_ARMING_ECON_MARKOUT_HORIZON_SECONDS", 300),
        arming_econ_min_probe_win_rate_net_rt=_env_float(
            "KEEL_ARMING_ECON_MIN_PROBE_WIN_RATE_NET_RT", 0.55
        ),
        arming_econ_min_avg_net_rt_bps=_env_float("KEEL_ARMING_ECON_MIN_AVG_NET_RT_BPS", 0.0),
        shadow_near_probe=_env_bool("KEEL_SHADOW_NEAR_PROBE", False),
        shadow_near_probe_cooldown_seconds=_env_int("KEEL_SHADOW_NEAR_PROBE_COOLDOWN_SECONDS", 900),
        shadow_near_probe_max_missing=_env_int("KEEL_SHADOW_NEAR_PROBE_MAX_MISSING", 2),
        shadow_near_probe_min_confidence=_env_float("KEEL_SHADOW_NEAR_PROBE_MIN_CONFIDENCE", 0.0),
        shadow_near_probe_min_edge_bps=_env_optional_float("KEEL_SHADOW_NEAR_PROBE_MIN_EDGE_BPS"),
        shadow_near_probe_edge_mode=_env_shadow_near_probe_edge_mode(),
        rule_fire_cooldown_seconds=clamp_rule_fire_cooldown_seconds(
            _env_int("KEEL_RULE_FIRE_COOLDOWN_SECONDS", 900)
        ),
        shadow_fee_role=_env_shadow_fee_role(),
        shadow_maker_fee_bps=_env_optional_float("KEEL_SHADOW_MAKER_FEE_BPS"),
        shadow_taker_fee_bps=_env_optional_float("KEEL_SHADOW_TAKER_FEE_BPS"),
        cycle_interval_seconds=cycle_interval_seconds,
        observe_preset=observe_preset,
        instruments=_env_instruments(),
    )


def refresh_settings() -> Settings:
    """Clear cache and reload settings from environment / ``.env``."""
    global _DOTENV_LOADED
    _DOTENV_LOADED = False
    _DOTENV_VALUES.clear()
    get_settings.cache_clear()
    return get_settings()
