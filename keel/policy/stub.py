"""
Deterministic Stub / Rule decision policies for offline tests and paper cycles.

No LLM calls. Rule v3+: RSI + trend + MACD + EMA stack + adaptive volume_ratio
filters, soft RSI relax when the other four gates pass, and edge hints for
near-probe observability.
R5: real multi-TF trends (trend_15m/1h/4h); entry gate is 15m; optional hard
1h alignment via KEEL_RULE_REQUIRE_1H_TREND (default 0 = soft confirm only).
R6: when trend_1h_confirm and nearest side aligns, multiply edge_hint_bps by
KEEL_RULE_1H_EDGE_BOOST (default 1.25x, clamped 1.0–2.0; uplift capped +5 bps).
R7: near-signal (1–2 missing gates) edge_hint uses distance-to-threshold geometry
+ ATR so base hint is non-zero when close; full-gate EV path unchanged; 1h boost
still applied after base; probe fee hurdle (~10 bps) unchanged.
R8: recalibrate near-edge penalties for low-ATR regimes (BTC/ETH ~18–40 bps):
penalties scale with min(atr_bps, available_ev) so modest residuals leave room to
clear the 10 bps probe hurdle without spamming when far from gates; near p and
penalty coeffs are env-tunable; ≥3 missing still fail-closed; hurdle unchanged.
Q0: diagnose_rule_signal attaches structured near-signal gate diagnostics.
E2A: opt-in ``KEEL_RULE_VARIANT=trend_follow`` (default ``mean_revert``) —
force 15m+1h same-direction trend gates; RSI side gates become "not
overbought/oversold" (TF defaults 68/32) instead of mean-reversion extremes.
Policy name stays ``rule`` so E1 full_gate detection still counts fires.
E2B (trend_follow only): volume soft path without RSI extreme when
trend+macd+ema align (``volume_path=soft_tf``); small MACD histogram lag
tolerance via ``KEEL_RULE_TF_MACD_LAG_BPS`` (default 3.0, clamp 0–15).
mean_revert volume soft + strict MACD sign unchanged.
E3.1 (trend_follow only): ``KEEL_RULE_TF_REQUIRE_4H`` (default 1) enables the
4h leg; F9 ``KEEL_RULE_4H_MODE`` (default **hard**=E3.1 same-dir; **soft**=4h not-opposing)
(neutral OK if 1h aligned); ``hard`` restores E3.1 same-direction. Set
TF_REQUIRE_4H=0 for E2A 15m+1h-only. mean_revert ignores these envs.
F9 (trend_follow selectivity; keep LLM primary / rule shadow):
``KEEL_RULE_REQUIRE_15M_ALIGN`` (default 1) — 15m not-opposing;
``KEEL_RULE_ADX_MIN`` (default **15**, 0=off, fail-open if missing);
``KEEL_RULE_SHORT_RSI_MAX`` / ``KEEL_RULE_LONG_RSI_MIN`` (default 52/48)
RSI mid veto. Gates stamped into ``signal_diag`` / reason for rule_shadow audit.
F2a (trend_follow only): ``KEEL_RULE_TF_MAX_EXTENSION_ATR`` (default **0=off**,
clamp 0.5–5 when >0) rejects entries already extended vs ``ema_21`` in ATR
units — long ``(price-ema_21)/atr_14``, short ``(ema_21-price)/atr_14``. Gate
``extension_ok``; atr_14<=0 fail-closed when enabled. mean_revert unchanged.
F2b (trend_follow only): RSI pullback gate so we do not buy strength already
spent — ``KEEL_RULE_TF_PULLBACK`` master (default **0** off (F2b backtest over-filtered)); long
``rsi_14 <= KEEL_RULE_TF_RSI_PULLBACK_LONG_MAX`` (default 52); short
``rsi_14 >= KEEL_RULE_TF_RSI_PULLBACK_SHORT_MIN`` (default 48). Gate
``pullback_ok``; set master 0 to disable both. mean_revert unchanged.
F5 (opt-in): ``supertrend`` / ``donchian`` — public TradingView-style concepts
(ATR-band flip / prior-bar Donchian + EMA + volume SMA×k). Not Pine source.
Defaults leave live observe on ``.env`` variant (trend_follow). E0 freeze.
P1: ``KEEL_RULE_VARIANT=regime`` routes per bar: squeeze/shock → WAIT;
trend → TF-like gates (15m+1h, 4h default off); range → VWAP/%B fade.
P2: ``KEEL_RULE_VARIANT=score`` (aliases ``regime_score``) keeps P1 routing
but replaces AND with a 0–5 score (fire ≥4). Trend: HTF 1h, Supertrend,
EMA21/VWAP pullback on the discount side, MACD, volume (volume is a point,
not a hard veto). RSI only vetoes chase (long>70 / short<30). Range: must
have extreme %B + VWAP adverse, then ≥4 of five tighter points. P1
``regime`` stays bit-for-bit. mean_revert / trend_follow unchanged.
P5: ``KEEL_RULE_VARIANT=squeeze_release`` (aliases ``sqz`` / ``squeeze``)
fires only on the first expansion bar after a TTM squeeze, with Supertrend
+ 1h agreement and RSI chase veto. Not a per-bar AND router. Measurement
only — live default stays ``mean_revert``.
"""
from __future__ import annotations

from keel.config.settings import _env
from typing import Any

from keel.factors.market_data import MarketSnapshot
from keel.factors.technical import classify_market_regime, detect_squeeze_release
from keel.domain.decision import Decision, validate_decision
from keel.policy.protocol import DecisionPolicy, PolicyContext, PolicyResult
from keel.policy.tv_rules import (
    adx_regime_ok,
    diagnose_donchian,
    diagnose_supertrend,
    tv_long_short_ok,
)

# Geometry mirrors near-probe / historical rule fires (TP 2.2 ATR, SL 1.0 ATR).
_TP_ATR = 2.2
_SL_ATR = 1.0
_RULE_GATE_COUNT = 5
# R6: 1h-confirm edge_hint multiplier bounds + absolute uplift cap (bps).
_1H_EDGE_BOOST_DEFAULT = 1.25
_1H_EDGE_BOOST_MIN = 1.0
_1H_EDGE_BOOST_MAX = 2.0
_1H_EDGE_BOOST_CAP_BPS = 5.0
# E2B: TF MACD lag tolerance (hist_bps within ±lag still OK).
_TF_MACD_LAG_BPS_DEFAULT = 3.0
_TF_MACD_LAG_BPS_MIN = 0.0
_TF_MACD_LAG_BPS_MAX = 15.0
# F2a: TF max extension from ema_21 in ATR units (0 disables).
_TF_MAX_EXTENSION_ATR_DEFAULT = 0.0
_TF_MAX_EXTENSION_ATR_MIN = 0.5
_TF_MAX_EXTENSION_ATR_MAX = 5.0
# F2b: TF RSI pullback (master off by default; F2b over-filtered).
_TF_PULLBACK_DEFAULT = False
_TF_RSI_PULLBACK_LONG_MAX_DEFAULT = 52.0
_TF_RSI_PULLBACK_SHORT_MIN_DEFAULT = 48.0
_TF_RSI_PULLBACK_MIN = 20.0
_TF_RSI_PULLBACK_MAX = 80.0
# F9: rule-side selectivity (LLM F8 parity for shadow quality).
_RULE_4H_MODE_DEFAULT = "hard"
_RULE_REQUIRE_15M_ALIGN_DEFAULT = True
_RULE_SHORT_RSI_MAX_DEFAULT = 52.0
_RULE_LONG_RSI_MIN_DEFAULT = 48.0
# P1 range fade (percent_b / VWAP / RSI).
_RANGE_PB_LONG_MAX_DEFAULT = 0.20
_RANGE_PB_SHORT_MIN_DEFAULT = 0.80
_RANGE_RSI_LONG_MAX_DEFAULT = 50.0
_RANGE_RSI_SHORT_MIN_DEFAULT = 50.0
_SHOCK_ATR_MULT_DEFAULT = 2.5
# P2 score variant (pre-declared; not fit on the P1 700-bar window).
_SCORE_MIN_DEFAULT = 4
_SCORE_MIN_LO = 3
_SCORE_MIN_HI = 5
_SCORE_PULLBACK_ATR_DEFAULT = 0.75
_SCORE_RSI_VETO_LONG_MAX_DEFAULT = 70.0
_SCORE_RSI_VETO_SHORT_MIN_DEFAULT = 30.0
_SCORE_RANGE_PB_LONG_MAX_DEFAULT = 0.15
_SCORE_RANGE_PB_SHORT_MIN_DEFAULT = 0.85
_SCORE_RANGE_RSI_LONG_MAX_DEFAULT = 40.0
_SCORE_RANGE_RSI_SHORT_MIN_DEFAULT = 60.0
# R7/R8: near-signal edge_hint geometry (distance-to-threshold + ATR).
# R8 defaults: slightly higher near p + softer penalty fracs so low-ATR
# (≈18–40 bps) can still clear the 10 bps probe hurdle when residuals are modest.
_NEAR_P1_DEFAULT = 0.50
_NEAR_P2_DEFAULT = 0.45
_RSI_ATR_SCALE_DEFAULT = 14.0  # RSI pts → severity vs pen_scale
_VOL_PENALTY_FRAC_DEFAULT = 0.25  # volume gap weight vs pen_scale (R8; was 0.40)
_BINARY_GATE_PENALTY_FRAC_DEFAULT = 0.25  # trend/macd/ema miss vs pen_scale (R8; was 0.30)
_RSI_PENALTY_COEF_DEFAULT = 1.0
_NEAR_P_MIN = 0.20
_NEAR_P_MAX = 0.65
_EDGE_HINT_ATR_CAP = 1.5  # never claim more than 1.5× atr_bps
_BINARY_MISSING_GATES = frozenset(
    {
        "trend_bullish",
        "trend_bearish",
        "macd_long_ok",
        "macd_short_ok",
        "ema_long_ok",
        "ema_short_ok",
        "extension_ok",
        "pullback_ok",
        "range_pb_ok",
        "range_vwap_ok",
        "range_rsi_ok",
        "regime_ok",
        "htf",
        "st",
        "pullback",
        "momentum",
        "volume",
        "st_not_against",
        "rsi_veto",
        "score_ok",
        "squeeze_release",
        "st_ok",
        "htf_ok",
        "adx_ok",
        "rsi_mid_ok",
        "tf15_align_ok",
    }
)


def _env_float(key: str, default: float) -> float:
    raw = (_env(key, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = (_env(key, "") or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _clamp_near_p(raw: float, default: float) -> float:
    """Clamp near-mode win-prob into a conservative band."""
    try:
        p = float(raw)
    except (TypeError, ValueError):
        p = default
    if p != p:  # NaN
        return default
    return max(_NEAR_P_MIN, min(_NEAR_P_MAX, p))


def _edge_hint_geometry() -> dict[str, float]:
    """
    R8 env-tunable near-edge geometry coefficients (documented defaults).

    Penalties scale with ``min(atr_bps, atr_bps * sized_EV)`` so modest residuals
    leave fee-clearing room in low-ATR regimes without lowering the 10 bps hurdle.
    """
    return {
        "near_p1": _clamp_near_p(
            _env_float("KEEL_RULE_EDGE_NEAR_P1", _NEAR_P1_DEFAULT), _NEAR_P1_DEFAULT
        ),
        "near_p2": _clamp_near_p(
            _env_float("KEEL_RULE_EDGE_NEAR_P2", _NEAR_P2_DEFAULT), _NEAR_P2_DEFAULT
        ),
        "rsi_atr_scale": max(
            1.0, _env_float("KEEL_RULE_EDGE_RSI_ATR_SCALE", _RSI_ATR_SCALE_DEFAULT)
        ),
        "vol_penalty_frac": max(
            0.0,
            _env_float("KEEL_RULE_EDGE_VOL_PENALTY_FRAC", _VOL_PENALTY_FRAC_DEFAULT),
        ),
        "binary_penalty_frac": max(
            0.0,
            _env_float(
                "KEEL_RULE_EDGE_BINARY_PENALTY_FRAC", _BINARY_GATE_PENALTY_FRAC_DEFAULT
            ),
        ),
        "rsi_penalty_coef": max(
            0.0,
            _env_float("KEEL_RULE_EDGE_RSI_PENALTY_COEF", _RSI_PENALTY_COEF_DEFAULT),
        ),
    }


def _rule_variant() -> str:
    """
    E2A / F5: rule semantics variant (env ``KEEL_RULE_VARIANT``).

    - ``mean_revert`` (default when unset): RSI extremes + soft 1h confirm.
    - ``trend_follow``: hard 15m+1h (+4h when TF_REQUIRE_4H); RSI = not OB/OS.
    - ``supertrend`` (F5): ATR-band direction flip + HTF filter (opt-in).
    - ``donchian`` (F5): prior-bar Donchian breakout + EMA + volume SMA×k.
    - ``regime``: P1 router (squeeze/shock WAIT; trend=TF-like 15m+1h;
      range=VWAP/%B fade).
    - ``score``: P2 scored router (P1 regimes; fire when score ≥4).
    - ``squeeze_release``: P5 event entry (prev squeeze, now expand + ST+1h).
    Unknown values fall back to ``mean_revert``. Live default remains whatever
    ``.env`` sets (observe keeps ``trend_follow``); code default for unset
    stays ``mean_revert``.
    """
    raw = (_env("KEEL_RULE_VARIANT") or "mean_revert").strip().lower()
    if raw in ("trend_follow", "trend-follow", "tf"):
        return "trend_follow"
    if raw in ("supertrend", "super_trend", "super-trend", "st"):
        return "supertrend"
    if raw in ("donchian", "donchian_breakout", "donchian-breakout", "dc"):
        return "donchian"
    if raw in ("regime", "router", "regime_router"):
        return "regime"
    if raw in ("score", "regime_score", "scored"):
        return "score"
    if raw in ("squeeze_release", "sqz", "squeeze"):
        return "squeeze_release"
    return "mean_revert"


def resolve_rule_variant() -> str:
    """Public alias for status/config echo (same as diagnose ``rule_variant``)."""
    return _rule_variant()


def resolve_tf_require_4h() -> bool:
    """
    E3.1 / F5 / F9: whether the 4h leg is enabled for status/config echo.

    True under ``trend_follow`` / ``supertrend`` / ``donchian`` when
    ``KEEL_RULE_TF_REQUIRE_4H`` is on (default on). Soft vs hard interpretation
    is ``resolve_rule_4h_mode()``. Always False under ``mean_revert``.
    """
    if _rule_variant() not in ("trend_follow", "supertrend", "donchian"):
        return False
    return _env_bool("KEEL_RULE_TF_REQUIRE_4H", True)


def resolve_rule_4h_mode() -> str:
    """
    F9: ``soft`` (default) | ``hard`` | ``off``.

    Mapping with ``KEEL_RULE_TF_REQUIRE_4H``:
    - REQUIRE_4H=0 → ``off`` (E2A 15m+1h only; mode env ignored)
    - REQUIRE_4H=1 + MODE=soft → 1h same-dir; 4h not-opposing (neutral OK)
    - REQUIRE_4H=1 + MODE=hard → E3.1 same-direction 4h
    """
    if not resolve_tf_require_4h():
        return "off"
    raw = (_env("KEEL_RULE_4H_MODE") or _RULE_4H_MODE_DEFAULT).strip().lower()
    if raw in ("hard", "strict", "same"):
        return "hard"
    if raw in ("soft", "not_oppose", "not-opposing", "neutral_ok"):
        return "soft"
    return "soft"


def resolve_rule_require_15m_align() -> bool:
    """F9: 15m not-opposing gate (default on) under trend_follow only."""
    if _rule_variant() != "trend_follow":
        return False
    return _env_bool("KEEL_RULE_REQUIRE_15M_ALIGN", _RULE_REQUIRE_15M_ALIGN_DEFAULT)


def resolve_rule_short_rsi_max() -> float:
    """F9: short blocked when RSI >= this (default 52)."""
    return _env_float("KEEL_RULE_SHORT_RSI_MAX", _RULE_SHORT_RSI_MAX_DEFAULT)


def resolve_rule_long_rsi_min() -> float:
    """F9: long blocked when RSI <= this (default 48)."""
    return _env_float("KEEL_RULE_LONG_RSI_MIN", _RULE_LONG_RSI_MIN_DEFAULT)


def _rsi_mid_ok_side(
    snapshot: MarketSnapshot,
    *,
    side: str,
    enabled: bool,
    short_max: float,
    long_min: float,
) -> bool:
    """
    F9 RSI mid veto (LLM F8 parity).

    Short blocked if RSI >= short_max; long blocked if RSI <= long_min.
    When disabled: always True.
    """
    if not enabled:
        return True
    rsi = float(getattr(snapshot, "rsi_14", 50.0) or 50.0)
    if side == "short":
        return bool(rsi < float(short_max))
    return bool(rsi > float(long_min))


def _clamp_tf_max_extension_atr(raw: float) -> float:
    """
    F2a: clamp ``KEEL_RULE_TF_MAX_EXTENSION_ATR``.

    ``<=0`` disables the filter (returns 0). Otherwise clamp into [0.5, 5].
    """
    try:
        m = float(raw)
    except (TypeError, ValueError):
        m = _TF_MAX_EXTENSION_ATR_DEFAULT
    if m != m:  # NaN
        return _TF_MAX_EXTENSION_ATR_DEFAULT
    if m <= 0.0:
        return 0.0
    return max(_TF_MAX_EXTENSION_ATR_MIN, min(_TF_MAX_EXTENSION_ATR_MAX, m))


def resolve_tf_max_extension_atr() -> float:
    """
    F2a: effective TF max-extension ATR for status/config echo.

    Under ``trend_follow``: clamped env (default 0=off; >0 enables).
    Always 0.0 under ``mean_revert`` (env ignored).
    """
    if _rule_variant() != "trend_follow":
        return 0.0
    return _clamp_tf_max_extension_atr(
        _env_float("KEEL_RULE_TF_MAX_EXTENSION_ATR", _TF_MAX_EXTENSION_ATR_DEFAULT)
    )


def _clamp_tf_rsi_pullback(raw: float, default: float) -> float:
    """F2b: clamp RSI pullback threshold into [20, 80]."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = default
    if v != v:  # NaN
        return default
    return max(_TF_RSI_PULLBACK_MIN, min(_TF_RSI_PULLBACK_MAX, v))


def _clamp_score_min(raw: float) -> int:
    """P2: fire threshold in {3,4,5}; default 4."""
    try:
        v = int(round(float(raw)))
    except (TypeError, ValueError):
        v = _SCORE_MIN_DEFAULT
    return max(_SCORE_MIN_LO, min(_SCORE_MIN_HI, v))


def _clamp_score_pullback_atr(raw: float) -> float:
    """P2: max ATR distance to EMA21/VWAP for the pullback point."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = _SCORE_PULLBACK_ATR_DEFAULT
    if v != v or v <= 0.0:
        return _SCORE_PULLBACK_ATR_DEFAULT
    return max(0.25, min(2.0, v))


def resolve_tf_pullback_enabled() -> bool:
    """
    F2b: whether TF RSI pullback gate is active.

    True only under ``trend_follow`` when ``KEEL_RULE_TF_PULLBACK`` is on
    (default False). Always False under ``mean_revert``.
    """
    if _rule_variant() != "trend_follow":
        return False
    return _env_bool("KEEL_RULE_TF_PULLBACK", _TF_PULLBACK_DEFAULT)


def resolve_tf_rsi_pullback_long_max() -> float:
    """F2b: effective long pullback RSI max (0 under mean_revert / when off)."""
    if not resolve_tf_pullback_enabled():
        return 0.0
    return _clamp_tf_rsi_pullback(
        _env_float(
            "KEEL_RULE_TF_RSI_PULLBACK_LONG_MAX",
            _TF_RSI_PULLBACK_LONG_MAX_DEFAULT,
        ),
        _TF_RSI_PULLBACK_LONG_MAX_DEFAULT,
    )


def resolve_tf_rsi_pullback_short_min() -> float:
    """F2b: effective short pullback RSI min (0 under mean_revert / when off)."""
    if not resolve_tf_pullback_enabled():
        return 0.0
    return _clamp_tf_rsi_pullback(
        _env_float(
            "KEEL_RULE_TF_RSI_PULLBACK_SHORT_MIN",
            _TF_RSI_PULLBACK_SHORT_MIN_DEFAULT,
        ),
        _TF_RSI_PULLBACK_SHORT_MIN_DEFAULT,
    )


def _tf_pullback_ok(
    snapshot: MarketSnapshot,
    *,
    side: str,
    enabled: bool,
    long_max: float,
    short_min: float,
) -> bool:
    """
    F2b RSI pullback gate.

    long: rsi_14 <= long_max; short: rsi_14 >= short_min.
    When disabled: always True.
    """
    if not enabled:
        return True
    rsi = float(getattr(snapshot, "rsi_14", 0.0) or 0.0)
    if side == "short":
        return bool(rsi >= float(short_min))
    return bool(rsi <= float(long_max))


def _tf_extension_metrics(
    snapshot: MarketSnapshot,
    *,
    side: str,
    max_extension_atr: float,
) -> tuple[bool, float | None]:
    """
    F2a extension vs ema_21 in ATR units.

    long: (price - ema_21) / atr_14
    short: (ema_21 - price) / atr_14

    When ``max_extension_atr <= 0`` the filter is off (ok=True) but extension_atr
    is still reported when atr_14 > 0. When enabled and atr_14 <= 0: fail-closed.
    """
    atr = float(getattr(snapshot, "atr_14", 0.0) or 0.0)
    price = float(getattr(snapshot, "price", 0.0) or 0.0)
    ema = float(getattr(snapshot, "ema_21", 0.0) or 0.0)
    ext: float | None
    if atr <= 0.0:
        ext = None
        if max_extension_atr <= 0.0:
            return True, ext
        return False, ext
    if side == "short":
        ext = (ema - price) / atr
    else:
        ext = (price - ema) / atr
    if max_extension_atr <= 0.0:
        return True, ext
    return bool(ext <= max_extension_atr), ext


def _rule_thresholds() -> dict[str, float | bool | str]:
    """
    Rule v3+ thresholds (env-overridable, backward-compatible KEEL_RULE_*).

    Default ``KEEL_RULE_MIN_VOLUME_RATIO`` lowered 1.0 → 0.5: live okx_public
    relative volume is right-skewed (p50≈0.36–0.40, p90≈0.86), so ≥1.0 almost
    never passed and dominated missing-gate histograms (~85%+). 0.5 sits above
    the median (filters dead bars) without requiring an above-average spike.

    After v3, okx_public missing shifts to RSI side gates (esp. ``rsi_short_ok``):
    observed RSI sits mid-band (p50≈45–46, max often <50), so hard short ≥58
    almost never clears. Defaults widen modestly 42/58 → 45/55, and a soft RSI
    relax path (other four gates + slightly looser band) mirrors volume soft
    confirm without flooding every bar.
    """
    variant = _rule_variant()
    th: dict[str, float | bool | str] = {
        # Hard RSI bands (mean-reversion): modestly widened after v3 RSI bottleneck.
        "rsi_long_max": _env_float("KEEL_RULE_RSI_LONG_MAX", 45.0),
        "rsi_short_min": _env_float("KEEL_RULE_RSI_SHORT_MIN", 55.0),
        # Hard volume floor (relative volume vs 20-bar mean).
        "min_vol": _env_float("KEEL_RULE_MIN_VOLUME_RATIO", 0.5),
        # Adaptive: also pass when last-bar percentile rank ≥ this (0 disables).
        "min_vol_percentile": _env_float("KEEL_RULE_MIN_VOLUME_PERCENTILE", 55.0),
        # Soft volume confirmation when other 4 gates + strong RSI extreme.
        "vol_soft_enable": _env_bool("KEEL_RULE_VOLUME_SOFT_ENABLE", True),
        "vol_soft_floor": _env_float("KEEL_RULE_VOLUME_SOFT_FLOOR", 0.35),
        "rsi_soft_long_max": _env_float("KEEL_RULE_RSI_SOFT_LONG_MAX", 35.0),
        "rsi_soft_short_min": _env_float("KEEL_RULE_RSI_SOFT_SHORT_MIN", 65.0),
        # Soft RSI relax: other 4 (trend/macd/ema/volume) + looser RSI band.
        "rsi_relax_enable": _env_bool("KEEL_RULE_RSI_RELAX_ENABLE", True),
        "rsi_relax_long_max": _env_float("KEEL_RULE_RSI_RELAX_LONG_MAX", 48.0),
        "rsi_relax_short_min": _env_float("KEEL_RULE_RSI_RELAX_SHORT_MIN", 52.0),
        # R5: hard-require 1h trend same direction as 15m (default off = soft confirm).
        "require_1h_trend": _env_bool("KEEL_RULE_REQUIRE_1H_TREND", False),
        # R6: multiplicative edge_hint boost when 1h confirms nearest side (default 1.25x).
        "edge_1h_boost": _env_float("KEEL_RULE_1H_EDGE_BOOST", _1H_EDGE_BOOST_DEFAULT),
        "rule_variant": variant,
        # E2B: TF-only MACD lag (0 under mean_revert).
        "tf_macd_lag_bps": 0.0,
        # E3.1: TF-only 4h hard-require (ignored / False under mean_revert).
        "require_4h_trend": False,
        # F2a: TF-only max extension ATR (0 under mean_revert / when disabled).
        "max_extension_atr": 0.0,
        # F2b: TF-only RSI pullback (off under mean_revert).
        "tf_pullback_enabled": False,
        "rsi_pullback_long_max": 0.0,
        "rsi_pullback_short_min": 0.0,
        # F9: TF-only selectivity (off / ignored under mean_revert).
        "rule_4h_mode": "off",
        "require_15m_align": False,
        "rsi_mid_enabled": False,
        "short_rsi_max": _RULE_SHORT_RSI_MAX_DEFAULT,
        "long_rsi_min": _RULE_LONG_RSI_MIN_DEFAULT,
    }
    if variant == "trend_follow":
        # E2A: RSI = not overbought (long) / not oversold (short); force 15m+1h.
        th["rsi_long_max"] = _env_float("KEEL_RULE_TF_RSI_LONG_MAX", 68.0)
        th["rsi_short_min"] = _env_float("KEEL_RULE_TF_RSI_SHORT_MIN", 32.0)
        th["require_1h_trend"] = True
        # Mean-reversion soft RSI relax does not apply under trend-follow.
        th["rsi_relax_enable"] = False
        # E2B: small adverse MACD hist tolerance (bps of price).
        th["tf_macd_lag_bps"] = _clamp_tf_macd_lag_bps(
            _env_float("KEEL_RULE_TF_MACD_LAG_BPS", _TF_MACD_LAG_BPS_DEFAULT)
        )
        # E3.1/F9: 4h leg enabled (default on; 0 = E2A 15m+1h only).
        th["require_4h_trend"] = _env_bool("KEEL_RULE_TF_REQUIRE_4H", True)
        # F9 soft/hard 4h interpretation (soft = not-opposing; hard = E3.1 same-dir).
        _m = (_env("KEEL_RULE_4H_MODE") or _RULE_4H_MODE_DEFAULT).strip().lower()
        if _m in ("hard", "strict", "same"):
            th["rule_4h_mode"] = "hard"
        else:
            th["rule_4h_mode"] = "soft"
        # F9: 15m not-opposing (default on).
        th["require_15m_align"] = _env_bool(
            "KEEL_RULE_REQUIRE_15M_ALIGN", _RULE_REQUIRE_15M_ALIGN_DEFAULT
        )
        # F9: RSI mid veto (default on for TF; KEEL_RULE_RSI_MID=0 disables).
        th["short_rsi_max"] = _env_float(
            "KEEL_RULE_SHORT_RSI_MAX", _RULE_SHORT_RSI_MAX_DEFAULT
        )
        th["long_rsi_min"] = _env_float(
            "KEEL_RULE_LONG_RSI_MIN", _RULE_LONG_RSI_MIN_DEFAULT
        )
        mid_master = _env_bool("KEEL_RULE_RSI_MID", True)
        # Extreme bands also disable (legacy replay pack uses 100/0).
        th["rsi_mid_enabled"] = bool(
            mid_master
            and float(th["short_rsi_max"]) < 100.0
            and float(th["long_rsi_min"]) > 0.0
        )
        # F2a: reject already-extended TF entries (default 0=off; >0 enables).
        th["max_extension_atr"] = _clamp_tf_max_extension_atr(
            _env_float(
                "KEEL_RULE_TF_MAX_EXTENSION_ATR", _TF_MAX_EXTENSION_ATR_DEFAULT
            )
        )
        # F2b: RSI pullback (master default off).
        pb_on = _env_bool("KEEL_RULE_TF_PULLBACK", _TF_PULLBACK_DEFAULT)
        th["tf_pullback_enabled"] = pb_on
        th["rsi_pullback_long_max"] = _clamp_tf_rsi_pullback(
            _env_float(
                "KEEL_RULE_TF_RSI_PULLBACK_LONG_MAX",
                _TF_RSI_PULLBACK_LONG_MAX_DEFAULT,
            ),
            _TF_RSI_PULLBACK_LONG_MAX_DEFAULT,
        )
        th["rsi_pullback_short_min"] = _clamp_tf_rsi_pullback(
            _env_float(
                "KEEL_RULE_TF_RSI_PULLBACK_SHORT_MIN",
                _TF_RSI_PULLBACK_SHORT_MIN_DEFAULT,
            ),
            _TF_RSI_PULLBACK_SHORT_MIN_DEFAULT,
        )
    elif variant == "regime":
        # P1: TF-like directional gates for TREND path; 4h default off.
        th["rsi_long_max"] = _env_float("KEEL_RULE_TF_RSI_LONG_MAX", 68.0)
        th["rsi_short_min"] = _env_float("KEEL_RULE_TF_RSI_SHORT_MIN", 32.0)
        th["require_1h_trend"] = True
        th["rsi_relax_enable"] = False
        th["tf_macd_lag_bps"] = _clamp_tf_macd_lag_bps(
            _env_float("KEEL_RULE_TF_MACD_LAG_BPS", _TF_MACD_LAG_BPS_DEFAULT)
        )
        th["require_4h_trend"] = _env_bool("KEEL_RULE_TF_REQUIRE_4H", False)
        th["range_pb_long_max"] = _env_float(
            "KEEL_RULE_RANGE_PB_LONG_MAX", _RANGE_PB_LONG_MAX_DEFAULT
        )
        th["range_pb_short_min"] = _env_float(
            "KEEL_RULE_RANGE_PB_SHORT_MIN", _RANGE_PB_SHORT_MIN_DEFAULT
        )
        th["range_rsi_long_max"] = _env_float(
            "KEEL_RULE_RANGE_RSI_LONG_MAX", _RANGE_RSI_LONG_MAX_DEFAULT
        )
        th["range_rsi_short_min"] = _env_float(
            "KEEL_RULE_RANGE_RSI_SHORT_MIN", _RANGE_RSI_SHORT_MIN_DEFAULT
        )
        th["shock_atr_mult"] = _env_float(
            "KEEL_RULE_SHOCK_ATR_MULT", _SHOCK_ATR_MULT_DEFAULT
        )
        th["range_volume_soft"] = True
    elif variant == "score":
        # P2: same router as P1; 4h still a bonus (default off), not an AND.
        th["rsi_long_max"] = _env_float("KEEL_RULE_TF_RSI_LONG_MAX", 68.0)
        th["rsi_short_min"] = _env_float("KEEL_RULE_TF_RSI_SHORT_MIN", 32.0)
        th["require_1h_trend"] = True
        th["rsi_relax_enable"] = False
        th["tf_macd_lag_bps"] = _clamp_tf_macd_lag_bps(
            _env_float("KEEL_RULE_TF_MACD_LAG_BPS", _TF_MACD_LAG_BPS_DEFAULT)
        )
        th["require_4h_trend"] = _env_bool("KEEL_RULE_TF_REQUIRE_4H", False)
        th["score_min"] = _clamp_score_min(
            _env_float("KEEL_RULE_SCORE_MIN", float(_SCORE_MIN_DEFAULT))
        )
        th["score_pullback_atr"] = _clamp_score_pullback_atr(
            _env_float("KEEL_RULE_SCORE_PULLBACK_ATR", _SCORE_PULLBACK_ATR_DEFAULT)
        )
        th["score_rsi_veto_long_max"] = _clamp_tf_rsi_pullback(
            _env_float(
                "KEEL_RULE_SCORE_RSI_VETO_LONG_MAX",
                _SCORE_RSI_VETO_LONG_MAX_DEFAULT,
            ),
            _SCORE_RSI_VETO_LONG_MAX_DEFAULT,
        )
        th["score_rsi_veto_short_min"] = _clamp_tf_rsi_pullback(
            _env_float(
                "KEEL_RULE_SCORE_RSI_VETO_SHORT_MIN",
                _SCORE_RSI_VETO_SHORT_MIN_DEFAULT,
            ),
            _SCORE_RSI_VETO_SHORT_MIN_DEFAULT,
        )
        th["range_pb_long_max"] = _env_float(
            "KEEL_RULE_RANGE_PB_LONG_MAX", _SCORE_RANGE_PB_LONG_MAX_DEFAULT
        )
        th["range_pb_short_min"] = _env_float(
            "KEEL_RULE_RANGE_PB_SHORT_MIN", _SCORE_RANGE_PB_SHORT_MIN_DEFAULT
        )
        th["range_rsi_long_max"] = _env_float(
            "KEEL_RULE_RANGE_RSI_LONG_MAX", _SCORE_RANGE_RSI_LONG_MAX_DEFAULT
        )
        th["range_rsi_short_min"] = _env_float(
            "KEEL_RULE_RANGE_RSI_SHORT_MIN", _SCORE_RANGE_RSI_SHORT_MIN_DEFAULT
        )
        th["shock_atr_mult"] = _env_float(
            "KEEL_RULE_SHOCK_ATR_MULT", _SHOCK_ATR_MULT_DEFAULT
        )
        # Range volume is a score point; no soft_range auto-pass (discipline).
        th["range_volume_soft"] = _env_bool("KEEL_RULE_RANGE_VOLUME_SOFT", False)
    elif variant == "squeeze_release":
        # P5: event entry; 4h is not an AND. RSI chase veto reuses score knobs.
        th["rsi_long_max"] = _env_float("KEEL_RULE_TF_RSI_LONG_MAX", 68.0)
        th["rsi_short_min"] = _env_float("KEEL_RULE_TF_RSI_SHORT_MIN", 32.0)
        th["require_1h_trend"] = True
        th["rsi_relax_enable"] = False
        th["require_4h_trend"] = False
        th["score_rsi_veto_long_max"] = _clamp_tf_rsi_pullback(
            _env_float(
                "KEEL_RULE_SCORE_RSI_VETO_LONG_MAX",
                _SCORE_RSI_VETO_LONG_MAX_DEFAULT,
            ),
            _SCORE_RSI_VETO_LONG_MAX_DEFAULT,
        )
        th["score_rsi_veto_short_min"] = _clamp_tf_rsi_pullback(
            _env_float(
                "KEEL_RULE_SCORE_RSI_VETO_SHORT_MIN",
                _SCORE_RSI_VETO_SHORT_MIN_DEFAULT,
            ),
            _SCORE_RSI_VETO_SHORT_MIN_DEFAULT,
        )
        th["shock_atr_mult"] = _env_float(
            "KEEL_RULE_SHOCK_ATR_MULT", _SHOCK_ATR_MULT_DEFAULT
        )
    return th


def _missing_gate_suffix(diag: dict[str, Any]) -> str:
    """F9: stamp WAIT gate fails into reason for Monitor / rule_shadow audit."""
    missing = diag.get("missing")
    if not isinstance(missing, list) or not missing:
        return ""
    return " | gates=" + ",".join(str(x) for x in missing)


def _rule_reason_prefix(diag: dict[str, Any], *, fired: bool, side: str) -> str:
    variant = str(diag.get("rule_variant") or "")
    if variant == "squeeze_release":
        if fired:
            return f"rule {side} squeeze_release"
        return "no rule signal squeeze_release" + _missing_gate_suffix(diag)
    if variant in ("regime", "score"):
        path = str(diag.get("regime") or diag.get("regime_path") or "")
        score = diag.get("score")
        score_bit = ""
        if score is not None:
            score_bit = f" score={int(score)}/{int(diag.get('score_min') or 0)}"
        if fired:
            return f"rule {side} {variant} {path}{score_bit}".strip()
        return (
            f"no rule signal {variant} {path}{score_bit}".strip()
            + _missing_gate_suffix(diag)
        )
    if variant == "trend_follow":
        if fired:
            return f"rule {side} trend_follow"
        return "no rule signal trend_follow" + _missing_gate_suffix(diag)
    if fired:
        return f"rule {side}"
    return "no rule signal" + _missing_gate_suffix(diag)


def _factor_reason(snapshot: MarketSnapshot, prefix: str) -> str:
    t1h = getattr(snapshot, "trend_1h", "neutral") or "neutral"
    t4h = getattr(snapshot, "trend_4h", "neutral") or "neutral"
    return (
        f"{prefix} rsi={snapshot.rsi_14:.1f} "
        f"trend15m={snapshot.trend_15m} trend1h={t1h} trend4h={t4h} "
        f"macd_h={snapshot.macd_histogram:.4f} "
        f"ema9={snapshot.ema_9:.4f} ema21={snapshot.ema_21:.4f} "
        f"vol={snapshot.volume_ratio:.2f}"
    )


_LONG_GATES = (
    "rsi_long_ok",
    "trend_bullish",
    "macd_long_ok",
    "ema_long_ok",
    "volume_ok",
)
_SHORT_GATES = (
    "rsi_short_ok",
    "trend_bearish",
    "macd_short_ok",
    "ema_short_ok",
    "volume_ok",
)
_RANGE_LONG_GATES = (
    "range_pb_ok",
    "range_vwap_ok",
    "range_rsi_ok",
    "volume_ok",
)
_RANGE_SHORT_GATES = (
    "range_pb_ok",
    "range_vwap_ok",
    "range_rsi_ok",
    "volume_ok",
)


def _snapshot_regime(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
) -> str:
    """Prefer enrich-time ``snapshot.regime``; else classify from fields."""
    raw = str(getattr(snapshot, "regime", "") or "").strip().lower()
    if raw in ("squeeze", "trend", "range", "shock"):
        return raw
    bar_range = 0.0
    candles = getattr(snapshot, "candles_15m", None) or []
    if candles:
        last = candles[-1]
        try:
            bar_range = float(last.high) - float(last.low)
        except (TypeError, ValueError, AttributeError):
            bar_range = 0.0
    shock_mult = float(th.get("shock_atr_mult") or _SHOCK_ATR_MULT_DEFAULT)
    return classify_market_regime(
        squeeze=bool(getattr(snapshot, "squeeze", False)),
        supertrend_direction=int(getattr(snapshot, "supertrend_direction", 0) or 0),
        trend_1h=str(getattr(snapshot, "trend_1h", "neutral") or "neutral"),
        bar_range=bar_range,
        atr=float(getattr(snapshot, "atr_14", 0.0) or 0.0),
        shock_atr_mult=shock_mult,
    )


def _range_volume_ok(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
) -> tuple[bool, float, str]:
    """Volume for range fades: hard / percentile / soft floor (no directional AND)."""
    min_vol = float(th["min_vol"])
    min_pct = float(th["min_vol_percentile"])
    soft_enable = bool(th["vol_soft_enable"])
    soft_floor = float(th["vol_soft_floor"])
    ratio = float(snapshot.volume_ratio or 0.0)
    pct = getattr(snapshot, "volume_percentile", None)
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None
    if ratio >= min_vol:
        return True, float(min_vol), "hard"
    if min_pct > 0 and pct_f is not None and pct_f >= min_pct:
        return True, float(min_pct), "percentile"
    range_soft = bool(th.get("range_volume_soft", True))
    if soft_enable and range_soft and ratio >= soft_floor:
        return True, float(soft_floor), "soft_range"
    return False, float(min_vol), "fail"


def _diagnose_regime_block(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
    regime: str,
) -> dict[str, Any]:
    """Squeeze / shock: fail-closed WAIT with ``missing=['regime_ok']``."""
    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0
    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "regime": regime,
        "regime_ok": False,
        "regime_path": regime,
        "rule_variant": str(th.get("rule_variant") or "regime"),
        "nearest": "none",
        "missing": ["data_valid"] if not data_ok else ["regime_ok"],
        "near_ready": False,
        "rsi_14": snapshot.rsi_14,
        "volume_ok": False,
        "volume_ratio": snapshot.volume_ratio,
        "bb_percent_b": float(getattr(snapshot, "bb_percent_b", 0.5) or 0.5),
        "squeeze": bool(getattr(snapshot, "squeeze", False)),
        "supertrend_direction": int(getattr(snapshot, "supertrend_direction", 0) or 0),
        "trend_15m": str(snapshot.trend_15m or "neutral"),
        "trend_1h": str(getattr(snapshot, "trend_1h", "neutral") or "neutral"),
        "trend_4h": str(getattr(snapshot, "trend_4h", "neutral") or "neutral"),
        "require_1h_trend": bool(th.get("require_1h_trend", False)),
        "require_4h_trend": bool(th.get("require_4h_trend", False)),
    }
    gates.update(
        apply_1h_edge_boost(
            _edge_hints(
                snapshot,
                list(gates["missing"]),
                nearest="none",
                th=th,
            ),
            trend_1h_confirm=False,
            nearest="none",
            trend_15m=str(snapshot.trend_15m or "neutral"),
            boost_mult=float(th["edge_1h_boost"]),
        )
    )
    return gates


def _diagnose_squeeze_release(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
) -> dict[str, Any]:
    """
    P5 event entry: first expansion bar after TTM squeeze + Supertrend + 1h.

    Must: squeeze_prev and not squeeze_now; Supertrend ±1; 1h same side.
    RSI only vetoes chase (long>70 / short<30). Volume is not a gate.
    Expansion bar is the event — shock WAIT is not applied (P1 shock would
    drop the release bar). Fail-closed if Supertrend or 1h is missing.
    """
    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0
    squeeze_now = bool(getattr(snapshot, "squeeze", False))
    squeeze_prev = bool(getattr(snapshot, "squeeze_prev", False))
    release_attr = getattr(snapshot, "squeeze_release", None)
    if release_attr is None:
        released = detect_squeeze_release(
            squeeze_now=squeeze_now, squeeze_prev=squeeze_prev
        )
    else:
        released = bool(release_attr)
    try:
        st = int(getattr(snapshot, "supertrend_direction", 0) or 0)
    except (TypeError, ValueError):
        st = 0
    trend_1h = str(getattr(snapshot, "trend_1h", "neutral") or "neutral")
    rsi = float(snapshot.rsi_14)
    veto_long_max = float(
        th.get("score_rsi_veto_long_max") or _SCORE_RSI_VETO_LONG_MAX_DEFAULT
    )
    veto_short_min = float(
        th.get("score_rsi_veto_short_min") or _SCORE_RSI_VETO_SHORT_MIN_DEFAULT
    )
    st_ok = st != 0
    htf_long = trend_1h == "bullish"
    htf_short = trend_1h == "bearish"
    htf_ok = (st > 0 and htf_long) or (st < 0 and htf_short)
    rsi_long_ok = rsi <= veto_long_max
    rsi_short_ok = rsi >= veto_short_min
    if st > 0:
        rsi_veto_ok = rsi_long_ok
    elif st < 0:
        rsi_veto_ok = rsi_short_ok
    else:
        rsi_veto_ok = True

    missing: list[str] = []
    nearest = "none"
    if not data_ok:
        missing = ["data_valid"]
    elif not released:
        missing = ["squeeze_release"]
    else:
        if not st_ok:
            missing.append("st_ok")
        if not htf_ok:
            missing.append("htf_ok")
        if not rsi_veto_ok:
            missing.append("rsi_veto")
        if not missing:
            nearest = "long" if st > 0 else "short"

    if released and not missing:
        regime_label = "squeeze_release"
    elif squeeze_now:
        regime_label = "squeeze"
    else:
        regime_label = "wait"

    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "regime": regime_label,
        "regime_ok": bool(released),
        "regime_path": "squeeze_release",
        "rule_variant": "squeeze_release",
        "nearest": nearest,
        "missing": missing,
        "near_ready": False,
        "squeeze": squeeze_now,
        "squeeze_prev": squeeze_prev,
        "squeeze_release": released,
        "supertrend_direction": st,
        "st_ok": st_ok,
        "htf_ok": htf_ok,
        "rsi_14": rsi,
        "rsi_veto_ok": rsi_veto_ok,
        "rsi_long_ok": rsi_long_ok,
        "rsi_short_ok": rsi_short_ok,
        "volume_ok": True,
        "volume_ratio": snapshot.volume_ratio,
        "trend_15m": str(snapshot.trend_15m or "neutral"),
        "trend_1h": trend_1h,
        "trend_4h": str(getattr(snapshot, "trend_4h", "neutral") or "neutral"),
        "require_1h_trend": True,
        "require_4h_trend": False,
        "trend_bullish": st > 0 and htf_long,
        "trend_bearish": st < 0 and htf_short,
        "macd_long_ok": False,
        "macd_short_ok": False,
        "ema_long_ok": False,
        "ema_short_ok": False,
    }
    gates.update(
        apply_1h_edge_boost(
            _edge_hints(
                snapshot,
                list(missing),
                nearest=nearest,
                th=th,
            ),
            trend_1h_confirm=bool(htf_ok),
            nearest=nearest,
            trend_15m=str(snapshot.trend_15m or "neutral"),
            boost_mult=float(th["edge_1h_boost"]),
        )
    )
    return gates


def _diagnose_range_signal(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
    regime: str,
) -> dict[str, Any]:
    """P1 RANGE path: fade %B extremes toward VWAP (no EMA/MACD/4h AND)."""
    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0
    pb_long_max = float(th.get("range_pb_long_max") or _RANGE_PB_LONG_MAX_DEFAULT)
    pb_short_min = float(th.get("range_pb_short_min") or _RANGE_PB_SHORT_MIN_DEFAULT)
    rsi_long_max = float(th.get("range_rsi_long_max") or _RANGE_RSI_LONG_MAX_DEFAULT)
    rsi_short_min = float(th.get("range_rsi_short_min") or _RANGE_RSI_SHORT_MIN_DEFAULT)
    try:
        pb = float(getattr(snapshot, "bb_percent_b", 0.5) or 0.5)
    except (TypeError, ValueError):
        pb = 0.5
    try:
        bias = float(getattr(snapshot, "vwap_bias_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        bias = 0.0
    rsi = float(snapshot.rsi_14)
    volume_ok, vol_th, vol_path = _range_volume_ok(snapshot, th)

    long_pb = pb <= pb_long_max
    short_pb = pb >= pb_short_min
    long_vwap = bias <= 0.0
    short_vwap = bias >= 0.0
    long_rsi = rsi <= rsi_long_max
    short_rsi = rsi >= rsi_short_min

    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "regime": regime,
        "regime_ok": True,
        "regime_path": "range",
        "rule_variant": str(th.get("rule_variant") or "regime"),
        "bb_percent_b": pb,
        "vwap_bias_pct": bias,
        "squeeze": bool(getattr(snapshot, "squeeze", False)),
        "rsi_14": rsi,
        "volume_ok": volume_ok,
        "volume_ratio": snapshot.volume_ratio,
        "volume_threshold": vol_th,
        "volume_path": vol_path,
        "volume_soft_pass": vol_path == "soft_range",
        "range_pb_long_max": pb_long_max,
        "range_pb_short_min": pb_short_min,
        "trend_15m": str(snapshot.trend_15m or "neutral"),
        "trend_1h": str(getattr(snapshot, "trend_1h", "neutral") or "neutral"),
        "trend_4h": str(getattr(snapshot, "trend_4h", "neutral") or "neutral"),
        "require_1h_trend": False,
        "require_4h_trend": False,
        "rsi_long_ok": long_rsi,
        "rsi_short_ok": short_rsi,
        "trend_bullish": False,
        "trend_bearish": False,
        "macd_long_ok": False,
        "macd_short_ok": False,
        "ema_long_ok": False,
        "ema_short_ok": False,
    }

    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
        gates["range_pb_ok"] = False
        gates["range_vwap_ok"] = False
        gates["range_rsi_ok"] = False
        gates.update(
            apply_1h_edge_boost(
                _edge_hints(snapshot, ["data_valid"], nearest="none", th=th),
                trend_1h_confirm=False,
                nearest="none",
                trend_15m=str(snapshot.trend_15m or "neutral"),
                boost_mult=float(th["edge_1h_boost"]),
            )
        )
        return gates

    long_flags = {
        "range_pb_ok": long_pb,
        "range_vwap_ok": long_vwap,
        "range_rsi_ok": long_rsi,
        "volume_ok": volume_ok,
    }
    short_flags = {
        "range_pb_ok": short_pb,
        "range_vwap_ok": short_vwap,
        "range_rsi_ok": short_rsi,
        "volume_ok": volume_ok,
    }
    long_missing = [g for g in _RANGE_LONG_GATES if not long_flags[g]]
    short_missing = [g for g in _RANGE_SHORT_GATES if not short_flags[g]]
    n_long, n_short = len(long_missing), len(short_missing)
    if n_long == 0:
        nearest, missing, side_flags = "long", [], long_flags
    elif n_short == 0:
        nearest, missing, side_flags = "short", [], short_flags
    elif n_long <= n_short:
        nearest, missing, side_flags = "long", long_missing, long_flags
    else:
        nearest, missing, side_flags = "short", short_missing, short_flags

    gates["range_pb_ok"] = bool(side_flags["range_pb_ok"])
    gates["range_vwap_ok"] = bool(side_flags["range_vwap_ok"])
    gates["range_rsi_ok"] = bool(side_flags["range_rsi_ok"])
    gates["nearest"] = nearest
    gates["missing"] = missing
    gates["near_ready"] = len(missing) == 1
    gates.update(
        apply_1h_edge_boost(
            _edge_hints(snapshot, list(missing), nearest=str(nearest), th=th),
            trend_1h_confirm=False,
            nearest=str(nearest),
            trend_15m=str(snapshot.trend_15m or "neutral"),
            boost_mult=float(th["edge_1h_boost"]),
        )
    )
    return gates


def _atr_dist_ema21(snapshot: MarketSnapshot) -> float:
    atr = float(getattr(snapshot, "atr_14", 0.0) or 0.0)
    if atr <= 0.0:
        return float("inf")
    return abs(float(snapshot.price) - float(snapshot.ema_21)) / atr


def _atr_dist_vwap(snapshot: MarketSnapshot) -> float:
    atr = float(getattr(snapshot, "atr_14", 0.0) or 0.0)
    price = float(getattr(snapshot, "price", 0.0) or 0.0)
    if atr <= 0.0 or price <= 0.0:
        return float("inf")
    try:
        bias = float(getattr(snapshot, "vwap_bias_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        bias = 0.0
    return abs(bias) / 100.0 * price / atr


def _score_macd_ok(snapshot: MarketSnapshot, th: dict[str, float | bool | str], *, side: str) -> bool:
    """MACD histogram same-side, with TF lag tolerance."""
    hist = float(snapshot.macd_histogram)
    price = float(snapshot.price or 0.0)
    hist_bps = (hist / price) * 1e4 if price > 0 else 0.0
    lag = float(th.get("tf_macd_lag_bps") or 0.0)
    if side == "long":
        return hist >= 0.0 or (lag > 0.0 and hist_bps >= -lag)
    return hist <= 0.0 or (lag > 0.0 and hist_bps <= lag)


def _score_volume_ok(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
    *,
    allow_soft: bool,
) -> tuple[bool, float, str]:
    th_vol = dict(th)
    th_vol["range_volume_soft"] = bool(allow_soft)
    return _range_volume_ok(snapshot, th_vol)


def _pick_scored_side(
    long_pack: dict[str, Any],
    short_pack: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Prefer the side that fires; else higher score; long wins ties."""
    lf = bool(long_pack.get("fires"))
    sf = bool(short_pack.get("fires"))
    if lf and not sf:
        return "long", long_pack
    if sf and not lf:
        return "short", short_pack
    if lf and sf:
        if int(short_pack.get("score") or 0) > int(long_pack.get("score") or 0):
            return "short", short_pack
        return "long", long_pack
    if int(short_pack.get("score") or 0) > int(long_pack.get("score") or 0):
        return "short", short_pack
    return "long", long_pack


def _score_attach_edge(
    snapshot: MarketSnapshot,
    gates: dict[str, Any],
    th: dict[str, float | bool | str],
) -> dict[str, Any]:
    missing = list(gates.get("missing") or [])
    nearest = str(gates.get("nearest") or "none")
    trend_1h = str(getattr(snapshot, "trend_1h", "neutral") or "neutral")
    confirm = (nearest == "long" and trend_1h == "bullish") or (
        nearest == "short" and trend_1h == "bearish"
    )
    gates.update(
        apply_1h_edge_boost(
            _edge_hints(snapshot, missing, nearest=nearest, th=th),
            trend_1h_confirm=confirm,
            nearest=nearest,
            trend_15m=str(snapshot.trend_15m or "neutral"),
            boost_mult=float(th["edge_1h_boost"]),
        )
    )
    return gates


def _diagnose_score_trend(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
    regime: str,
) -> dict[str, Any]:
    """
    P2 TREND path: 0–5 score, fire when ≥ score_min and RSI does not chase.

    Points: 1h align, Supertrend, EMA21/VWAP pullback on VWAP discount/premium,
    MACD, volume (volume is a point, not a hard AND). Must have HTF + Supertrend
    (a trend trade without both is not a trend trade).
    """
    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0
    score_min = int(th.get("score_min") or _SCORE_MIN_DEFAULT)
    pullback_atr = float(th.get("score_pullback_atr") or _SCORE_PULLBACK_ATR_DEFAULT)
    rsi_veto_long = float(
        th.get("score_rsi_veto_long_max") or _SCORE_RSI_VETO_LONG_MAX_DEFAULT
    )
    rsi_veto_short = float(
        th.get("score_rsi_veto_short_min") or _SCORE_RSI_VETO_SHORT_MIN_DEFAULT
    )
    rsi = float(snapshot.rsi_14)
    try:
        bias = float(getattr(snapshot, "vwap_bias_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        bias = 0.0
    st = int(getattr(snapshot, "supertrend_direction", 0) or 0)
    trend_1h = str(getattr(snapshot, "trend_1h", "neutral") or "neutral")
    near_struct = min(_atr_dist_ema21(snapshot), _atr_dist_vwap(snapshot)) <= pullback_atr
    vol_ok, vol_th, vol_path = _score_volume_ok(snapshot, th, allow_soft=True)

    def _pack(side: str) -> dict[str, Any]:
        if side == "long":
            bits = {
                "htf": trend_1h == "bullish",
                "st": st == 1,
                "pullback": near_struct and bias <= 0.0,
                "momentum": _score_macd_ok(snapshot, th, side="long"),
                "volume": vol_ok,
            }
            rsi_veto_ok = rsi <= rsi_veto_long
        else:
            bits = {
                "htf": trend_1h == "bearish",
                "st": st == -1,
                "pullback": near_struct and bias >= 0.0,
                "momentum": _score_macd_ok(snapshot, th, side="short"),
                "volume": vol_ok,
            }
            rsi_veto_ok = rsi >= rsi_veto_short
        score = int(sum(1 for v in bits.values() if v))
        must = bool(bits["htf"] and bits["st"])
        missing = [k for k, v in bits.items() if not v]
        if not rsi_veto_ok:
            missing = list(missing) + ["rsi_veto"]
        fires = data_ok and rsi_veto_ok and must and score >= score_min
        if not fires and (not must or score < score_min) and "score_ok" not in missing:
            missing = list(missing) + ["score_ok"]
        return {
            "bits": bits,
            "score": score,
            "rsi_veto_ok": rsi_veto_ok,
            "must": must,
            "missing": missing,
            "fires": fires,
        }

    long_pack = _pack("long")
    short_pack = _pack("short")
    nearest, chosen = _pick_scored_side(long_pack, short_pack)
    missing = [] if chosen["fires"] else list(chosen["missing"])
    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "regime": regime,
        "regime_ok": True,
        "regime_path": "trend",
        "rule_variant": "score",
        "score": int(chosen["score"]),
        "score_min": score_min,
        "score_breakdown": dict(chosen["bits"]),
        "rsi_veto_ok": bool(chosen["rsi_veto_ok"]),
        "bb_percent_b": float(getattr(snapshot, "bb_percent_b", 0.5) or 0.5),
        "vwap_bias_pct": bias,
        "squeeze": bool(getattr(snapshot, "squeeze", False)),
        "supertrend_direction": st,
        "rsi_14": rsi,
        "volume_ok": vol_ok,
        "volume_ratio": snapshot.volume_ratio,
        "volume_threshold": vol_th,
        "volume_path": vol_path,
        "trend_15m": str(snapshot.trend_15m or "neutral"),
        "trend_1h": trend_1h,
        "trend_4h": str(getattr(snapshot, "trend_4h", "neutral") or "neutral"),
        "require_1h_trend": True,
        "require_4h_trend": bool(th.get("require_4h_trend", False)),
        "trend_gate": "score",
        "rsi_long_ok": bool(long_pack["rsi_veto_ok"]),
        "rsi_short_ok": bool(short_pack["rsi_veto_ok"]),
        "trend_bullish": bool(long_pack["bits"]["htf"] and long_pack["bits"]["st"]),
        "trend_bearish": bool(short_pack["bits"]["htf"] and short_pack["bits"]["st"]),
        "macd_long_ok": bool(long_pack["bits"]["momentum"]),
        "macd_short_ok": bool(short_pack["bits"]["momentum"]),
        "ema_long_ok": bool(long_pack["bits"]["pullback"]),
        "ema_short_ok": bool(short_pack["bits"]["pullback"]),
        "nearest": nearest if data_ok else "none",
        "missing": ["data_valid"] if not data_ok else missing,
        "near_ready": (
            data_ok
            and not chosen["fires"]
            and bool(chosen.get("must"))
            and int(chosen["score"]) == score_min - 1
            and bool(chosen["rsi_veto_ok"])
        ),
    }
    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
    return _score_attach_edge(snapshot, gates, th)


def _diagnose_score_range(
    snapshot: MarketSnapshot,
    th: dict[str, float | bool | str],
    regime: str,
) -> dict[str, Any]:
    """
    P2 RANGE path: tighter fade. Must have extreme %B + VWAP adverse;
    fire when those two pass and total score ≥ min (of 5).
    """
    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0
    score_min = int(th.get("score_min") or _SCORE_MIN_DEFAULT)
    pb_long_max = float(th.get("range_pb_long_max") or _SCORE_RANGE_PB_LONG_MAX_DEFAULT)
    pb_short_min = float(th.get("range_pb_short_min") or _SCORE_RANGE_PB_SHORT_MIN_DEFAULT)
    rsi_long_max = float(th.get("range_rsi_long_max") or _SCORE_RANGE_RSI_LONG_MAX_DEFAULT)
    rsi_short_min = float(
        th.get("range_rsi_short_min") or _SCORE_RANGE_RSI_SHORT_MIN_DEFAULT
    )
    rsi_veto_long = float(
        th.get("score_rsi_veto_long_max") or _SCORE_RSI_VETO_LONG_MAX_DEFAULT
    )
    rsi_veto_short = float(
        th.get("score_rsi_veto_short_min") or _SCORE_RSI_VETO_SHORT_MIN_DEFAULT
    )
    try:
        pb = float(getattr(snapshot, "bb_percent_b", 0.5) or 0.5)
    except (TypeError, ValueError):
        pb = 0.5
    try:
        bias = float(getattr(snapshot, "vwap_bias_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        bias = 0.0
    rsi = float(snapshot.rsi_14)
    st = int(getattr(snapshot, "supertrend_direction", 0) or 0)
    vol_ok, vol_th, vol_path = _score_volume_ok(snapshot, th, allow_soft=False)

    def _pack(side: str) -> dict[str, Any]:
        if side == "long":
            bits = {
                "range_pb_ok": pb <= pb_long_max,
                "range_vwap_ok": bias <= 0.0,
                "range_rsi_ok": rsi <= rsi_long_max,
                "volume": vol_ok,
                "st_not_against": st != -1,
            }
            rsi_veto_ok = rsi <= rsi_veto_long
        else:
            bits = {
                "range_pb_ok": pb >= pb_short_min,
                "range_vwap_ok": bias >= 0.0,
                "range_rsi_ok": rsi >= rsi_short_min,
                "volume": vol_ok,
                "st_not_against": st != 1,
            }
            rsi_veto_ok = rsi >= rsi_veto_short
        score = int(sum(1 for v in bits.values() if v))
        must = bool(bits["range_pb_ok"] and bits["range_vwap_ok"])
        missing = [k for k, v in bits.items() if not v]
        if not rsi_veto_ok:
            missing = list(missing) + ["rsi_veto"]
        fires = data_ok and rsi_veto_ok and must and score >= score_min
        if not fires and (not must or score < score_min) and "score_ok" not in missing:
            missing = list(missing) + ["score_ok"]
        return {
            "bits": bits,
            "score": score,
            "rsi_veto_ok": rsi_veto_ok,
            "must": must,
            "missing": missing,
            "fires": fires,
        }

    long_pack = _pack("long")
    short_pack = _pack("short")
    nearest, chosen = _pick_scored_side(long_pack, short_pack)
    missing = [] if chosen["fires"] else list(chosen["missing"])
    bits = dict(chosen["bits"])
    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "regime": regime,
        "regime_ok": True,
        "regime_path": "range",
        "rule_variant": "score",
        "score": int(chosen["score"]),
        "score_min": score_min,
        "score_breakdown": bits,
        "rsi_veto_ok": bool(chosen["rsi_veto_ok"]),
        "bb_percent_b": pb,
        "vwap_bias_pct": bias,
        "squeeze": bool(getattr(snapshot, "squeeze", False)),
        "supertrend_direction": st,
        "rsi_14": rsi,
        "volume_ok": vol_ok,
        "volume_ratio": snapshot.volume_ratio,
        "volume_threshold": vol_th,
        "volume_path": vol_path,
        "range_pb_long_max": pb_long_max,
        "range_pb_short_min": pb_short_min,
        "range_pb_ok": bool(bits["range_pb_ok"]),
        "range_vwap_ok": bool(bits["range_vwap_ok"]),
        "range_rsi_ok": bool(bits["range_rsi_ok"]),
        "trend_15m": str(snapshot.trend_15m or "neutral"),
        "trend_1h": str(getattr(snapshot, "trend_1h", "neutral") or "neutral"),
        "trend_4h": str(getattr(snapshot, "trend_4h", "neutral") or "neutral"),
        "require_1h_trend": False,
        "require_4h_trend": False,
        "trend_gate": "score-range",
        "rsi_long_ok": bool(long_pack["bits"]["range_rsi_ok"]),
        "rsi_short_ok": bool(short_pack["bits"]["range_rsi_ok"]),
        "trend_bullish": False,
        "trend_bearish": False,
        "macd_long_ok": False,
        "macd_short_ok": False,
        "ema_long_ok": False,
        "ema_short_ok": False,
        "nearest": nearest if data_ok else "none",
        "missing": ["data_valid"] if not data_ok else missing,
        "near_ready": (
            data_ok
            and not chosen["fires"]
            and bool(chosen.get("must"))
            and int(chosen["score"]) == score_min - 1
            and bool(chosen["rsi_veto_ok"])
        ),
    }
    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
    return _score_attach_edge(snapshot, gates, th)


def _distance_penalty_bps(
    snapshot: MarketSnapshot,
    missing: list[str],
    *,
    atr_bps: float,
    available_ev_bps: float,
    th: dict[str, float | bool | str],
    geo: dict[str, float],
) -> tuple[float, dict[str, float], float]:
    """
    Residual distance-to-threshold in bps for near-signal edge hints (R7/R8).

    R8: penalties scale with ``pen_scale = min(atr_bps, available_ev_bps)`` so that
    when ATR is low and residuals are modest, ``atr*sized_EV - penalty`` can still
    clear ~10 bps; large residuals (e.g. RSI ≥15 pts) still wipe the hint.

    - RSI: pen_scale × (pts / rsi_atr_scale) × rsi_penalty_coef
    - volume: pen_scale × vol_penalty_frac × gap_frac (gap vs hard; ×0.5 if ≥ soft)
    - binary trend/macd/ema: pen_scale × binary_penalty_frac each
    """
    components: dict[str, float] = {}
    total = 0.0
    pen_scale = min(float(atr_bps), max(0.0, float(available_ev_bps)))
    rsi_scale = float(geo["rsi_atr_scale"])
    vol_frac = float(geo["vol_penalty_frac"])
    bin_frac = float(geo["binary_penalty_frac"])
    rsi_coef = float(geo["rsi_penalty_coef"])
    rsi_long_max = float(th["rsi_long_max"])
    rsi_short_min = float(th["rsi_short_min"])
    min_vol = float(th["min_vol"])
    soft_floor = float(th["vol_soft_floor"])
    try:
        rsi = float(snapshot.rsi_14)
    except (TypeError, ValueError):
        rsi = float("nan")
    try:
        ratio = float(snapshot.volume_ratio or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0

    for gate in missing:
        pen = 0.0
        if gate == "rsi_long_ok":
            if rsi == rsi:  # not NaN
                pts = max(0.0, rsi - rsi_long_max)
                pen = pen_scale * (pts / rsi_scale) * rsi_coef
        elif gate == "rsi_short_ok":
            if rsi == rsi:
                pts = max(0.0, rsi_short_min - rsi)
                pen = pen_scale * (pts / rsi_scale) * rsi_coef
        elif gate == "volume_ok":
            if min_vol > 0 and ratio < min_vol:
                gap = (min_vol - ratio) / min_vol
                if ratio >= soft_floor:
                    gap *= 0.5
                pen = pen_scale * vol_frac * min(gap, 1.5)
            else:
                # Failed via percentile/soft context with ratio ≥ hard — mild penalty.
                pen = pen_scale * vol_frac * 0.15
        elif gate in _BINARY_MISSING_GATES:
            pen = pen_scale * bin_frac
        else:
            pen = pen_scale * bin_frac
        components[gate] = float(pen)
        total += pen
    return float(total), components, float(pen_scale)


def _edge_hints(
    snapshot: MarketSnapshot,
    missing: list[str] | int,
    *,
    nearest: str = "none",
    th: dict[str, float | bool | str] | None = None,
) -> dict[str, Any]:
    """
    ATR-based expected move / edge hint for probe observability (does not change fee hurdle).

    R7/R8 formula (documented):
      atr_bps = (atr_14 / price) * 10_000
      expected_tp_bps = atr_bps * 2.2
      if missing empty (full fire):
          p = 0.70; sized_EV = 2.2*p - 1.0*(1-p)
          edge_hint_bps = max(0, atr_bps * sized_EV)   # mode=full
      elif 1–2 missing and nearest ∈ {long, short}:
          p = {1: near_p1, 2: near_p2}[n]   # R8 defaults 0.50 / 0.45
          sized_EV = 2.2*p - 1.0*(1-p)
          available_ev = atr_bps * sized_EV
          pen_scale = min(atr_bps, available_ev)       # R8 low-ATR room
          distance_penalty = Σ gate residuals vs pen_scale
          edge_hint_bps = max(0, available_ev - distance_penalty)  # mode=near
      else:
          edge_hint_bps = 0; mode=none
      cap: min(expected_tp_bps, 1.5 * atr_bps)

    R6 1h boost is applied **after** this base hint by ``apply_1h_edge_boost``.
    """
    empty: dict[str, Any] = {
        "atr_bps": None,
        "expected_tp_bps": None,
        "edge_hint_bps": None,
        "edge_hint_mode": "none",
        "edge_hint_sized_ev": None,
        "edge_hint_distance_penalty_bps": None,
        "edge_hint_distance_components": {},
        "edge_hint_penalty_scale_bps": None,
    }
    try:
        price = float(snapshot.price or 0.0)
        atr = float(snapshot.atr_14 or 0.0)
    except (TypeError, ValueError):
        return empty
    if price <= 0 or atr <= 0:
        return empty

    # Backward-compat: callers may still pass n_missing as int.
    if isinstance(missing, int):
        n_missing = max(0, int(missing))
        missing_list: list[str] = [f"gate_{i}" for i in range(n_missing)]
        # Int path has no gate identity — use synthetic binary penalties for near,
        # or full/none by count only.
        use_synthetic = True
    else:
        missing_list = list(missing or [])
        n_missing = len(missing_list)
        use_synthetic = False

    atr_bps = (atr / price) * 10_000.0
    expected_tp_bps = atr_bps * _TP_ATR
    thr = th if th is not None else _rule_thresholds()
    geo = _edge_hint_geometry()
    near_p_by_missing = {1: float(geo["near_p1"]), 2: float(geo["near_p2"])}

    nearest_s = str(nearest or "none")
    components: dict[str, float] = {}
    distance_penalty = 0.0
    pen_scale: float | None = None
    sized_ev: float | None = None
    mode = "none"
    edge_hint = 0.0

    if n_missing == 0:
        # Full-gate EV path (unchanged confidence 0.70).
        p = 0.70
        sized_ev = (_TP_ATR * p) - (_SL_ATR * (1.0 - p))
        edge_hint = max(0.0, atr_bps * sized_ev)
        mode = "full"
    elif n_missing in (1, 2) and nearest_s in ("long", "short"):
        p = float(near_p_by_missing[n_missing])
        sized_ev = (_TP_ATR * p) - (_SL_ATR * (1.0 - p))
        available_ev = atr_bps * sized_ev
        pen_scale = min(atr_bps, max(0.0, available_ev))
        bin_frac = float(geo["binary_penalty_frac"])
        if use_synthetic:
            # Int-only API: treat each missing as a mild binary residual.
            per = pen_scale * bin_frac
            distance_penalty = per * float(n_missing)
            components = {f"gate_{i}": per for i in range(n_missing)}
        else:
            distance_penalty, components, pen_scale = _distance_penalty_bps(
                snapshot,
                missing_list,
                atr_bps=atr_bps,
                available_ev_bps=available_ev,
                th=thr,
                geo=geo,
            )
        edge_hint = max(0.0, available_ev - distance_penalty)
        mode = "near"
    else:
        mode = "none"
        edge_hint = 0.0
        sized_ev = None
        distance_penalty = 0.0
        components = {}
        pen_scale = None

    if edge_hint > 0:
        edge_hint = min(edge_hint, expected_tp_bps, _EDGE_HINT_ATR_CAP * atr_bps)

    return {
        "atr_bps": float(atr_bps),
        "expected_tp_bps": float(expected_tp_bps),
        "edge_hint_bps": float(edge_hint),
        "edge_hint_mode": mode,
        "edge_hint_sized_ev": float(sized_ev) if sized_ev is not None else None,
        "edge_hint_distance_penalty_bps": (
            float(distance_penalty) if mode == "near" else (0.0 if mode == "full" else None)
        ),
        "edge_hint_distance_components": dict(components) if mode == "near" else {},
        "edge_hint_penalty_scale_bps": float(pen_scale) if mode == "near" else None,
    }


def _clamp_1h_edge_boost(raw: float) -> float:
    """Clamp KEEL_RULE_1H_EDGE_BOOST into [1.0, 2.0] so ops cannot invent huge edges."""
    try:
        m = float(raw)
    except (TypeError, ValueError):
        m = _1H_EDGE_BOOST_DEFAULT
    if m != m:  # NaN
        return _1H_EDGE_BOOST_DEFAULT
    return max(_1H_EDGE_BOOST_MIN, min(_1H_EDGE_BOOST_MAX, m))


def _clamp_tf_macd_lag_bps(raw: float) -> float:
    """Clamp KEEL_RULE_TF_MACD_LAG_BPS into [0, 15] (E2B)."""
    try:
        m = float(raw)
    except (TypeError, ValueError):
        m = _TF_MACD_LAG_BPS_DEFAULT
    if m != m:  # NaN
        return _TF_MACD_LAG_BPS_DEFAULT
    return max(_TF_MACD_LAG_BPS_MIN, min(_TF_MACD_LAG_BPS_MAX, m))


def apply_1h_edge_boost(
    hints: dict[str, float | None],
    *,
    trend_1h_confirm: bool,
    nearest: str,
    trend_15m: str,
    boost_mult: float,
) -> dict[str, Any]:
    """
    R6: multiply edge_hint_bps when 1h confirms and nearest side aligns with 15m.

    Formula (documented):
      mult = clamp(KEEL_RULE_1H_EDGE_BOOST, 1.0, 2.0)  # default 1.25
      if trend_1h_confirm and nearest∈{long,short} aligns with trend_15m:
          boosted = base * mult
          edge_hint_bps = min(boosted, base + 5.0)   # absolute uplift cap
      else:
          edge_hint_bps = base

    Does **not** change the near-probe fee hurdle (~10 bps taker RT).
    """
    out: dict[str, Any] = dict(hints)
    base = hints.get("edge_hint_bps")
    mult = _clamp_1h_edge_boost(boost_mult)
    out["edge_hint_boost_mult"] = float(mult)
    out["edge_hint_1h_boosted"] = False
    if base is None:
        return out
    try:
        base_f = float(base)
    except (TypeError, ValueError):
        return out
    if base_f != base_f or base_f < 0:
        return out
    # R7: no boost theater when base hint is already zero (live BTC symptom).
    if base_f == 0.0:
        out["edge_hint_bps"] = 0.0
        return out

    nearest_s = str(nearest or "")
    t15 = str(trend_15m or "neutral")
    aligns = (
        bool(trend_1h_confirm)
        and (
            (nearest_s == "long" and t15 == "bullish")
            or (nearest_s == "short" and t15 == "bearish")
        )
    )
    if not aligns or mult <= 1.0:
        out["edge_hint_bps"] = float(base_f)
        return out

    boosted = base_f * mult
    # Cap absolute uplift so a large mult cannot invent huge edges.
    capped = min(boosted, base_f + _1H_EDGE_BOOST_CAP_BPS)
    # Never claim more edge than the expected TP move.
    etp = hints.get("expected_tp_bps")
    try:
        etp_f = float(etp) if etp is not None else None
    except (TypeError, ValueError):
        etp_f = None
    if etp_f is not None and etp_f == etp_f and etp_f > 0:
        capped = min(capped, etp_f)
    out["edge_hint_bps"] = float(max(0.0, capped))
    out["edge_hint_1h_boosted"] = True
    out["edge_hint_bps_raw"] = float(base_f)
    return out


def _volume_gate(
    snapshot: MarketSnapshot,
    *,
    min_vol: float,
    min_vol_percentile: float,
    soft_enable: bool,
    soft_floor: float,
    other_four_ok: bool,
    rsi_extreme: bool,
    soft_tf_three_ok: bool = False,
) -> tuple[bool, float, bool, str]:
    """
    Evaluate volume_ok with hard / adaptive / soft paths.

    Returns (volume_ok, threshold_used, soft_pass, path).
    path ∈ {hard, percentile, soft_tf, soft, fail}.

    E2B: ``soft_tf`` (trend_follow only) passes when trend+macd+ema align and
    ratio ≥ soft_floor — no RSI extreme required. mean_revert still uses
    ``soft`` (other-four + RSI extreme).
    """
    ratio = float(snapshot.volume_ratio or 0.0)
    pct = getattr(snapshot, "volume_percentile", None)
    try:
        pct_f = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct_f = None

    if ratio >= min_vol:
        return True, float(min_vol), False, "hard"

    if min_vol_percentile > 0 and pct_f is not None and pct_f >= min_vol_percentile:
        return True, float(min_vol_percentile), False, "percentile"

    # E2B TF soft: three directional gates + soft floor (no RSI extreme).
    if soft_enable and soft_tf_three_ok and ratio >= soft_floor:
        return True, float(soft_floor), True, "soft_tf"

    if (
        soft_enable
        and other_four_ok
        and rsi_extreme
        and ratio >= soft_floor
    ):
        return True, float(soft_floor), True, "soft"

    # Threshold reported for diagnostics: hard floor (what still blocks).
    return False, float(min_vol), False, "fail"


def diagnose_rule_signal(snapshot: MarketSnapshot) -> dict[str, Any]:
    """
    Pure helper: structured rule-gate diagnostics for near-signal UX.

    Returns boolean gates, numeric snapshots, and ``nearest`` / ``missing``
    for the side closest to firing (fewer failed gates). Used by
    ``rule_based_decision`` (DRY) and unit-tested with crafted snapshots.

    Rule v3+ extras: ``volume_threshold``, ``volume_soft_pass``, ``volume_path``,
    ``volume_percentile``, ``rsi_soft_pass``, ``rsi_path``, ``near_ready``,
    ``atr_bps`` / ``expected_tp_bps`` / ``edge_hint_bps``.
    R5: ``trend_15m`` / ``trend_1h`` / ``trend_4h``, ``trend_gate``
    (``15m`` / ``15m+1h`` / ``15m+1h+4h``), ``trend_1h_confirm``,
    ``require_1h_trend``.
    R6: optional 1h-confirm ``edge_hint`` boost (``edge_hint_1h_boosted``,
    ``edge_hint_boost_mult``, ``edge_hint_bps_raw`` when applied).
    E2A: ``rule_variant`` (``mean_revert``|``trend_follow``); TF forces
    ``require_1h_trend`` and redefines RSI side gates (keys still
    ``rsi_long_ok`` / ``rsi_short_ok``).
    E2B: TF ``volume_path=soft_tf`` (trend+macd+ema, no RSI extreme);
    ``macd_lag_bps`` / ``macd_lag_ok`` for TF MACD hist lag tolerance.
    E3.1/F9: TF ``require_4h_trend`` / ``rule_4h_mode`` (soft=4h not-opposing;
    hard=same-dir) fold into ``trend_bullish``/``trend_bearish``;
    ``trend_gate`` like ``15m_align+1h+4h_soft``. F9 also stamps
    ``require_15m_align`` / ``tf15_align_ok``, ``rsi_mid_ok``, ``adx_ok``.
    F2a: TF ``max_extension_atr`` / ``extension_atr`` / ``extension_ok`` /
    ``extension_headroom_atr`` — reject when price is already extended vs
    ``ema_21`` beyond ``KEEL_RULE_TF_MAX_EXTENSION_ATR`` (default 0=off).
    F2b: TF ``pullback_ok`` / ``rsi_pullback_long_max`` /
    ``rsi_pullback_short_min`` — reject when RSI shows strength already spent
    (long ``rsi<=long_max`` default 52; short ``rsi>=short_min`` default 48);
    master ``KEEL_RULE_TF_PULLBACK`` (default 0 off; set 1 to enable).
    R7/R8: ``edge_hint_mode`` (``full``|``near``|``none``) plus
    ``edge_hint_sized_ev`` / ``edge_hint_distance_penalty_bps`` /
    ``edge_hint_distance_components`` / ``edge_hint_penalty_scale_bps``
    for near-signal geometry audit (R8 low-ATR pen_scale = min(atr, available_ev)).
    """
    th = _rule_thresholds()
    # F5: TradingView-inspired variants — dedicated gate semantics / reason codes.
    variant_early = str(th.get("rule_variant") or "mean_revert")
    if variant_early == "supertrend":
        gates = diagnose_supertrend(snapshot)
        gates.setdefault("edge_hint_bps", 0.0)
        gates.setdefault("edge_hint_mode", "none")
        gates.setdefault("near_ready", bool(gates.get("near_ready")))
        gates.setdefault("atr_bps", 0.0)
        gates.setdefault("expected_tp_bps", 0.0)
        return gates
    if variant_early == "donchian":
        gates = diagnose_donchian(snapshot)
        gates.setdefault("edge_hint_bps", 0.0)
        gates.setdefault("edge_hint_mode", "none")
        gates.setdefault("near_ready", bool(gates.get("near_ready")))
        gates.setdefault("atr_bps", 0.0)
        gates.setdefault("expected_tp_bps", 0.0)
        return gates
    rsi_long_max = float(th["rsi_long_max"])
    rsi_short_min = float(th["rsi_short_min"])
    min_vol = float(th["min_vol"])
    min_vol_percentile = float(th["min_vol_percentile"])
    soft_enable = bool(th["vol_soft_enable"])
    soft_floor = float(th["vol_soft_floor"])
    rsi_soft_long_max = float(th["rsi_soft_long_max"])
    rsi_soft_short_min = float(th["rsi_soft_short_min"])
    rsi_relax_enable = bool(th["rsi_relax_enable"])
    rsi_relax_long_max = float(th["rsi_relax_long_max"])
    rsi_relax_short_min = float(th["rsi_relax_short_min"])

    data_ok = bool(snapshot.data_valid) and snapshot.price > 0 and snapshot.atr_14 > 0

    variant_now = str(th.get("rule_variant") or "mean_revert")
    if variant_now == "squeeze_release" and data_ok:
        return _diagnose_squeeze_release(snapshot, th)
    if variant_now in ("regime", "score") and data_ok:
        regime_now = _snapshot_regime(snapshot, th)
        if regime_now in ("squeeze", "shock"):
            return _diagnose_regime_block(snapshot, th, regime_now)
        if regime_now == "range":
            if variant_now == "score":
                return _diagnose_score_range(snapshot, th, regime_now)
            return _diagnose_range_signal(snapshot, th, regime_now)
        if variant_now == "score":
            return _diagnose_score_trend(snapshot, th, regime_now)

    rsi_long_hard = snapshot.rsi_14 <= rsi_long_max
    rsi_short_hard = snapshot.rsi_14 >= rsi_short_min
    require_1h = bool(th["require_1h_trend"])
    require_4h = bool(th.get("require_4h_trend", False))
    rule_4h_mode = str(th.get("rule_4h_mode") or "off")
    if require_4h and rule_4h_mode not in ("soft", "hard"):
        rule_4h_mode = "soft"
    if not require_4h:
        rule_4h_mode = "off"
    require_15m_align = bool(th.get("require_15m_align", False))
    trend_15m = str(snapshot.trend_15m or "neutral")
    trend_1h = str(getattr(snapshot, "trend_1h", "neutral") or "neutral")
    trend_4h = str(getattr(snapshot, "trend_4h", "neutral") or "neutral")
    # Entry gate is always 15m; optional hard 1h / 4h same-direction confirmation.
    trend_15m_bullish = trend_15m == "bullish"
    trend_15m_bearish = trend_15m == "bearish"
    trend_1h_confirm_long = trend_1h == "bullish"
    trend_1h_confirm_short = trend_1h == "bearish"
    trend_4h_confirm_long = trend_4h == "bullish"
    trend_4h_confirm_short = trend_4h == "bearish"
    variant = str(th.get("rule_variant") or "mean_revert")
    is_tf = variant in ("trend_follow", "regime", "score")
    # F9: 15m not-opposing audit (LLM parity). TF uses this for the align gate.
    tf15_align_long = trend_15m != "bearish"
    tf15_align_short = trend_15m != "bullish"

    if variant == "trend_follow":
        # F9 TF HTF: 1h same-dir (forced); 15m same-dir when align on
        # (stricter than LLM not-opposing; still blocks opposing 15m);
        # 4h soft (not-opposing) or hard (same-dir) when REQUIRE_4H.
        t15_long_ok = trend_15m_bullish if require_15m_align else True
        t15_short_ok = trend_15m_bearish if require_15m_align else True
        t1h_long_ok = trend_1h_confirm_long  # TF always requires 1h
        t1h_short_ok = trend_1h_confirm_short
        if rule_4h_mode == "hard":
            t4h_long_ok = trend_4h_confirm_long
            t4h_short_ok = trend_4h_confirm_short
        elif rule_4h_mode == "soft":
            t4h_long_ok = trend_4h != "bearish"
            t4h_short_ok = trend_4h != "bullish"
        else:
            t4h_long_ok = True
            t4h_short_ok = True
        trend_bullish = t15_long_ok and t1h_long_ok and t4h_long_ok
        trend_bearish = t15_short_ok and t1h_short_ok and t4h_short_ok
        parts = []
        if require_15m_align:
            parts.append("15m_align")
        parts.append("1h")
        if rule_4h_mode == "soft":
            parts.append("4h_soft")
        elif rule_4h_mode == "hard":
            parts.append("4h")
        trend_gate = "+".join(parts) if parts else "off"
    elif require_1h and require_4h:
        # Non-TF (regime/score): preserve pre-F9 hard 4h when require_4h.
        if rule_4h_mode == "soft":
            trend_bullish = (
                trend_15m_bullish
                and trend_1h_confirm_long
                and (trend_4h != "bearish")
            )
            trend_bearish = (
                trend_15m_bearish
                and trend_1h_confirm_short
                and (trend_4h != "bullish")
            )
            trend_gate = "15m+1h+4h_soft"
        else:
            trend_bullish = (
                trend_15m_bullish and trend_1h_confirm_long and trend_4h_confirm_long
            )
            trend_bearish = (
                trend_15m_bearish and trend_1h_confirm_short and trend_4h_confirm_short
            )
            trend_gate = "15m+1h+4h"
    elif require_1h:
        trend_bullish = trend_15m_bullish and trend_1h_confirm_long
        trend_bearish = trend_15m_bearish and trend_1h_confirm_short
        trend_gate = "15m+1h"
    else:
        trend_bullish = trend_15m_bullish
        trend_bearish = trend_15m_bearish
        trend_gate = "15m"
    # Soft confirm flags: higher TF matches directional 15m (audit when soft).
    # Under TF F9 not-opposing, confirm still means same-dir higher TF.
    if trend_15m_bullish:
        trend_1h_confirm = trend_1h_confirm_long
        trend_4h_confirm = trend_4h_confirm_long
    elif trend_15m_bearish:
        trend_1h_confirm = trend_1h_confirm_short
        trend_4h_confirm = trend_4h_confirm_short
    elif variant == "trend_follow" and trend_1h_confirm_long:
        trend_1h_confirm = True
        trend_4h_confirm = trend_4h_confirm_long
    elif variant == "trend_follow" and trend_1h_confirm_short:
        trend_1h_confirm = True
        trend_4h_confirm = trend_4h_confirm_short
    else:
        trend_1h_confirm = False
        trend_4h_confirm = False
    # E2B: TF MACD lag — allow small adverse hist (bps of price); MR strict.
    macd_lag_bps = float(th.get("tf_macd_lag_bps") or 0.0)
    hist = float(snapshot.macd_histogram)
    price = float(snapshot.price or 0.0)
    hist_bps = (hist / price) * 1e4 if price > 0 else 0.0
    macd_long_strict = hist >= 0
    macd_short_strict = hist <= 0
    if is_tf and macd_lag_bps > 0:
        macd_long_ok = macd_long_strict or hist_bps >= -macd_lag_bps
        macd_short_ok = macd_short_strict or hist_bps <= macd_lag_bps
    else:
        macd_long_ok = macd_long_strict
        macd_short_ok = macd_short_strict
    macd_lag_ok = (macd_long_ok and not macd_long_strict) or (
        macd_short_ok and not macd_short_strict
    )
    ema_long_ok = snapshot.ema_9 >= snapshot.ema_21
    ema_short_ok = snapshot.ema_9 <= snapshot.ema_21

    # F2a: TF extension vs ema_21 (ATR units); MR / disabled → ok always.
    max_extension_atr = float(th.get("max_extension_atr") or 0.0)
    if not is_tf:
        max_extension_atr = 0.0
    extension_ok_long, extension_atr_long = _tf_extension_metrics(
        snapshot, side="long", max_extension_atr=max_extension_atr
    )
    extension_ok_short, extension_atr_short = _tf_extension_metrics(
        snapshot, side="short", max_extension_atr=max_extension_atr
    )

    # F2b: TF RSI pullback; MR / master-off → ok always.
    tf_pullback_enabled = bool(th.get("tf_pullback_enabled", False)) and is_tf
    rsi_pullback_long_max = float(th.get("rsi_pullback_long_max") or 0.0)
    rsi_pullback_short_min = float(th.get("rsi_pullback_short_min") or 0.0)
    pullback_ok_long = _tf_pullback_ok(
        snapshot,
        side="long",
        enabled=tf_pullback_enabled,
        long_max=rsi_pullback_long_max,
        short_min=rsi_pullback_short_min,
    )
    pullback_ok_short = _tf_pullback_ok(
        snapshot,
        side="short",
        enabled=tf_pullback_enabled,
        long_max=rsi_pullback_long_max,
        short_min=rsi_pullback_short_min,
    )

    # Volume soft uses *hard* RSI in the other-four (same as pre-relax).
    long_four = rsi_long_hard and trend_bullish and macd_long_ok and ema_long_ok
    short_four = rsi_short_hard and trend_bearish and macd_short_ok and ema_short_ok
    # Soft RSI extreme: tighter than the hard RSI band (for volume soft).
    rsi_extreme_long = snapshot.rsi_14 <= rsi_soft_long_max
    rsi_extreme_short = snapshot.rsi_14 >= rsi_soft_short_min
    # E2B TF soft volume: trend+macd+ema only (no RSI extreme).
    tf_three_long = trend_bullish and macd_long_ok and ema_long_ok
    tf_three_short = trend_bearish and macd_short_ok and ema_short_ok

    # Hard/percentile paths ignore side context; soft path needs other-4 + RSI extreme.
    # Try long soft context first, then short (identical for hard/percentile).
    candidates = [
        _volume_gate(
            snapshot,
            min_vol=min_vol,
            min_vol_percentile=min_vol_percentile,
            soft_enable=soft_enable,
            soft_floor=soft_floor,
            other_four_ok=long_four,
            rsi_extreme=rsi_extreme_long,
            soft_tf_three_ok=is_tf and tf_three_long,
        ),
        _volume_gate(
            snapshot,
            min_vol=min_vol,
            min_vol_percentile=min_vol_percentile,
            soft_enable=soft_enable,
            soft_floor=soft_floor,
            other_four_ok=short_four,
            rsi_extreme=rsi_extreme_short,
            soft_tf_three_ok=is_tf and tf_three_short,
        ),
    ]
    # Prefer hard > percentile > soft(_tf) > fail for diagnostics.
    _path_rank = {"hard": 0, "percentile": 1, "soft_tf": 2, "soft": 2, "fail": 3}
    best = min(candidates, key=lambda c: (_path_rank.get(c[3], 9), -int(c[0])))
    volume_ok, volume_threshold, volume_soft_pass, volume_path = best

    # Soft RSI relax (after volume): other four without RSI + looser band.
    # Mirrors volume soft confirm but loosens RSI when trend/macd/ema/volume align.
    long_other4 = trend_bullish and macd_long_ok and ema_long_ok and volume_ok
    short_other4 = trend_bearish and macd_short_ok and ema_short_ok and volume_ok
    rsi_long_soft = (
        rsi_relax_enable
        and long_other4
        and snapshot.rsi_14 <= rsi_relax_long_max
    )
    rsi_short_soft = (
        rsi_relax_enable
        and short_other4
        and snapshot.rsi_14 >= rsi_relax_short_min
    )
    rsi_long_ok = rsi_long_hard or rsi_long_soft
    rsi_short_ok = rsi_short_hard or rsi_short_soft
    if rsi_long_hard or rsi_short_hard:
        rsi_path = "hard"
        rsi_soft_pass = False
    elif rsi_long_soft or rsi_short_soft:
        rsi_path = "soft"
        rsi_soft_pass = True
    else:
        rsi_path = "fail"
        rsi_soft_pass = False

    vol_pct = getattr(snapshot, "volume_percentile", None)
    try:
        vol_pct_out: float | None = float(vol_pct) if vol_pct is not None else None
    except (TypeError, ValueError):
        vol_pct_out = None

    gates: dict[str, Any] = {
        "data_valid": data_ok,
        "rsi_long_ok": rsi_long_ok,
        "rsi_short_ok": rsi_short_ok,
        "trend_bullish": trend_bullish,
        "trend_bearish": trend_bearish,
        "macd_long_ok": macd_long_ok,
        "macd_short_ok": macd_short_ok,
        "macd_lag_bps": macd_lag_bps,
        "macd_lag_ok": macd_lag_ok,
        "ema_long_ok": ema_long_ok,
        "ema_short_ok": ema_short_ok,
        "volume_ok": volume_ok,
        "rsi_14": snapshot.rsi_14,
        "volume_ratio": snapshot.volume_ratio,
        "volume_threshold": volume_threshold,
        "volume_soft_pass": volume_soft_pass,
        "volume_path": volume_path,
        "volume_percentile": vol_pct_out,
        "rsi_soft_pass": rsi_soft_pass,
        "rsi_path": rsi_path,
        "ema_9": snapshot.ema_9,
        "ema_21": snapshot.ema_21,
        "macd_histogram": snapshot.macd_histogram,
        "trend_15m": trend_15m,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "trend_gate": trend_gate,
        "trend_1h_confirm": trend_1h_confirm,
        "trend_4h_confirm": trend_4h_confirm,
        "require_1h_trend": require_1h,
        "require_4h_trend": require_4h,
        "rule_4h_mode": rule_4h_mode,
        "require_15m_align": bool(require_15m_align) if variant == "trend_follow" else False,
        "tf15_align_ok": True,  # refined after nearest
        "tf15_align_long_ok": bool(tf15_align_long),
        "tf15_align_short_ok": bool(tf15_align_short),
        "rsi_mid_enabled": bool(th.get("rsi_mid_enabled", False)),
        "short_rsi_max": float(th.get("short_rsi_max") or _RULE_SHORT_RSI_MAX_DEFAULT),
        "long_rsi_min": float(th.get("long_rsi_min") or _RULE_LONG_RSI_MIN_DEFAULT),
        "rsi_mid_ok": True,  # refined after nearest
        "rule_variant": str(th.get("rule_variant") or "mean_revert"),
        "regime": (
            "trend" if variant == "regime" else str(getattr(snapshot, "regime", "") or "")
        ),
        "regime_ok": True,
        "regime_path": "trend" if variant == "regime" else "",
        "bb_percent_b": float(getattr(snapshot, "bb_percent_b", 0.5) or 0.5),
        "squeeze": bool(getattr(snapshot, "squeeze", False)),
        "max_extension_atr": max_extension_atr,
        # Side-agnostic placeholders; refined after nearest is known.
        "extension_atr": extension_atr_long,
        "extension_ok": extension_ok_long if max_extension_atr <= 0 else (
            extension_ok_long and extension_ok_short
        ),
        "extension_headroom_atr": None,
        # F2b pullback placeholders; refined after nearest.
        "tf_pullback_enabled": tf_pullback_enabled,
        "rsi_pullback_long_max": (
            rsi_pullback_long_max if tf_pullback_enabled else 0.0
        ),
        "rsi_pullback_short_min": (
            rsi_pullback_short_min if tf_pullback_enabled else 0.0
        ),
        "pullback_ok": pullback_ok_long if not tf_pullback_enabled else (
            pullback_ok_long and pullback_ok_short
        ),
    }

    if not data_ok:
        gates["nearest"] = "none"
        gates["missing"] = ["data_valid"]
        gates["near_ready"] = False
        gates.update(
            apply_1h_edge_boost(
                _edge_hints(
                    snapshot,
                    ["data_valid"],
                    nearest="none",
                    th=th,
                ),
                trend_1h_confirm=False,
                nearest="none",
                trend_15m=trend_15m,
                boost_mult=float(th["edge_1h_boost"]),
            )
        )
        return gates

    long_missing = [g for g in _LONG_GATES if not gates[g]]
    short_missing = [g for g in _SHORT_GATES if not gates[g]]
    n_long, n_short = len(long_missing), len(short_missing)

    if n_long == 0 and n_short == 0:
        nearest, missing = "long", []
    elif n_long == 0:
        nearest, missing = "long", []
    elif n_short == 0:
        nearest, missing = "short", []
    elif n_long < n_short:
        nearest, missing = "long", long_missing
    elif n_short < n_long:
        nearest, missing = "short", short_missing
    else:
        nearest, missing = "long", long_missing

    gates["nearest"] = nearest
    # F2a: fold extension_ok into missing for the nearest / full-gate side.
    if max_extension_atr > 0:
        if not extension_ok_long and "extension_ok" not in long_missing:
            long_missing = list(long_missing) + ["extension_ok"]
        if not extension_ok_short and "extension_ok" not in short_missing:
            short_missing = list(short_missing) + ["extension_ok"]
        # Recompute nearest/missing with extension included (same tie-break).
        n_long, n_short = len(long_missing), len(short_missing)
        if n_long == 0 and n_short == 0:
            nearest, missing = "long", []
        elif n_long == 0:
            nearest, missing = "long", []
        elif n_short == 0:
            nearest, missing = "short", []
        elif n_long < n_short:
            nearest, missing = "long", long_missing
        elif n_short < n_long:
            nearest, missing = "short", short_missing
        else:
            nearest, missing = "long", long_missing
    # F2b: fold pullback_ok into missing for the nearest / full-gate side.
    if tf_pullback_enabled:
        if not pullback_ok_long and "pullback_ok" not in long_missing:
            long_missing = list(long_missing) + ["pullback_ok"]
        if not pullback_ok_short and "pullback_ok" not in short_missing:
            short_missing = list(short_missing) + ["pullback_ok"]
        n_long, n_short = len(long_missing), len(short_missing)
        if n_long == 0 and n_short == 0:
            nearest, missing = "long", []
        elif n_long == 0:
            nearest, missing = "long", []
        elif n_short == 0:
            nearest, missing = "short", []
        elif n_long < n_short:
            nearest, missing = "long", long_missing
        elif n_short < n_long:
            nearest, missing = "short", short_missing
        else:
            nearest, missing = "long", long_missing
    # F6/F9: ADX regime gate for TF (F9 default min=15; fail-open if missing).
    adx_info = adx_regime_ok(snapshot) if is_tf else {
        "adx": None,
        "adx_plus_di": 0.0,
        "adx_minus_di": 0.0,
        "adx_period": 14,
        "adx_min": 0.0,
        "adx_enabled": False,
        "adx_ok": True,
        "adx_source": "off",
        "adx_fail_open": False,
    }
    adx_ok = bool(adx_info.get("adx_ok", True))
    gates.update(adx_info)
    if is_tf and bool(adx_info.get("adx_enabled")):
        if not adx_ok and "adx_ok" not in long_missing:
            long_missing = list(long_missing) + ["adx_ok"]
        if not adx_ok and "adx_ok" not in short_missing:
            short_missing = list(short_missing) + ["adx_ok"]
        n_long, n_short = len(long_missing), len(short_missing)
        if n_long == 0 and n_short == 0:
            nearest, missing = "long", []
        elif n_long == 0:
            nearest, missing = "long", []
        elif n_short == 0:
            nearest, missing = "short", []
        elif n_long < n_short:
            nearest, missing = "long", long_missing
        elif n_short < n_long:
            nearest, missing = "short", short_missing
        else:
            nearest, missing = "long", long_missing

    # F9: RSI mid veto (TF only; short if RSI>=max, long if RSI<=min).
    rsi_mid_enabled = bool(th.get("rsi_mid_enabled", False)) and variant == "trend_follow"
    short_rsi_max = float(th.get("short_rsi_max") or _RULE_SHORT_RSI_MAX_DEFAULT)
    long_rsi_min = float(th.get("long_rsi_min") or _RULE_LONG_RSI_MIN_DEFAULT)
    rsi_mid_ok_long = _rsi_mid_ok_side(
        snapshot,
        side="long",
        enabled=rsi_mid_enabled,
        short_max=short_rsi_max,
        long_min=long_rsi_min,
    )
    rsi_mid_ok_short = _rsi_mid_ok_side(
        snapshot,
        side="short",
        enabled=rsi_mid_enabled,
        short_max=short_rsi_max,
        long_min=long_rsi_min,
    )
    if rsi_mid_enabled:
        if not rsi_mid_ok_long and "rsi_mid_ok" not in long_missing:
            long_missing = list(long_missing) + ["rsi_mid_ok"]
        if not rsi_mid_ok_short and "rsi_mid_ok" not in short_missing:
            short_missing = list(short_missing) + ["rsi_mid_ok"]
        n_long, n_short = len(long_missing), len(short_missing)
        if n_long == 0 and n_short == 0:
            nearest, missing = "long", []
        elif n_long == 0:
            nearest, missing = "long", []
        elif n_short == 0:
            nearest, missing = "short", []
        elif n_long < n_short:
            nearest, missing = "long", long_missing
        elif n_short < n_long:
            nearest, missing = "short", short_missing
        else:
            nearest, missing = "long", long_missing

    if nearest == "short":
        extension_atr = extension_atr_short
        extension_ok = extension_ok_short
        pullback_ok = pullback_ok_short
        rsi_mid_ok = rsi_mid_ok_short
        tf15_align_ok = tf15_align_short
    else:
        extension_atr = extension_atr_long
        extension_ok = extension_ok_long
        pullback_ok = pullback_ok_long
        rsi_mid_ok = rsi_mid_ok_long
        tf15_align_ok = tf15_align_long
    # Recompute missing for the chosen nearest after side-specific folds.
    if nearest == "short":
        missing = list(short_missing)
    elif nearest == "long":
        missing = list(long_missing)
    else:
        missing = list(missing)
    gates["nearest"] = nearest
    gates["missing"] = missing
    gates["extension_atr"] = extension_atr
    gates["extension_ok"] = extension_ok
    gates["pullback_ok"] = pullback_ok
    gates["adx_ok"] = adx_ok
    gates["rsi_mid_ok"] = bool(rsi_mid_ok)
    gates["rsi_mid_enabled"] = bool(rsi_mid_enabled)
    gates["tf15_align_ok"] = bool(tf15_align_ok)
    if max_extension_atr > 0 and extension_atr is not None:
        gates["extension_headroom_atr"] = float(max_extension_atr) - float(
            extension_atr
        )
    else:
        gates["extension_headroom_atr"] = None
    # Staged near_ready: only volume blocks (soft floor) OR only RSI blocks within relax band.
    near_vol = (
        missing == ["volume_ok"]
        and float(snapshot.volume_ratio or 0.0) >= soft_floor
    )
    near_rsi_long = (
        missing == ["rsi_long_ok"]
        and rsi_relax_enable
        and snapshot.rsi_14 <= rsi_relax_long_max
    )
    near_rsi_short = (
        missing == ["rsi_short_ok"]
        and rsi_relax_enable
        and snapshot.rsi_14 >= rsi_relax_short_min
    )
    near_extension = (
        missing == ["extension_ok"]
        and max_extension_atr > 0
        and extension_atr is not None
        and float(extension_atr) <= float(max_extension_atr) + 0.25
    )
    # Soft near: only pullback missing and RSI within ~3 pts of threshold.
    near_pullback = False
    if tf_pullback_enabled and missing == ["pullback_ok"]:
        rsi = float(snapshot.rsi_14)
        if nearest == "long":
            near_pullback = rsi <= float(rsi_pullback_long_max) + 3.0
        elif nearest == "short":
            near_pullback = rsi >= float(rsi_pullback_short_min) - 3.0
    gates["near_ready"] = (
        near_vol or near_rsi_long or near_rsi_short or near_extension or near_pullback
    )
    gates.update(
        apply_1h_edge_boost(
            _edge_hints(
                snapshot,
                list(missing),
                nearest=str(nearest),
                th=th,
            ),
            trend_1h_confirm=bool(trend_1h_confirm),
            nearest=str(nearest),
            trend_15m=trend_15m,
            boost_mult=float(th["edge_1h_boost"]),
        )
    )
    return gates


def rule_based_decision(snapshot: MarketSnapshot) -> Decision:
    """
    Deterministic rule policy v3+ (no LLM).

    Long: RSI ≤ long_max + bullish(15m[+1h]) + MACD hist ≥ 0 + ema_9 ≥ ema_21 + volume_ok
    Short: RSI ≥ short_min + bearish(15m[+1h]) + MACD hist ≤ 0 + ema_9 ≤ ema_21 + volume_ok

    Trend entry gate is ``trend_15m``; set ``KEEL_RULE_REQUIRE_1H_TREND=1`` to also
    require ``trend_1h`` same direction (default 0 keeps soft confirm in signal_diag only).

    ``volume_ok`` (Rule v3): ratio ≥ min_vol (default 0.5), OR last-bar volume
    percentile ≥ min percentile (default 55), OR soft confirmation when the
    other four gates pass with a strong RSI extreme and ratio ≥ soft floor.

    ``rsi_*_ok`` soft relax (v3+): when trend+macd+ema+volume already pass,
    RSI may clear via a slightly looser band (default long ≤48 / short ≥52).

    Produces valid RR >= 2 geometry when a signal fires so the risk/execution
    path is exercised end-to-end. Offline-testable via crafted MarketSnapshot.
    Attaches ``signal_diag`` (from ``diagnose_rule_signal``) on every decision.
    Near-probe fee hurdle (10 bps taker RT) is unchanged — edge hints are audit-only.
    R6 may boost ``edge_hint_bps`` when 1h confirms nearest side (still does not
    lower the fee hurdle). R7/R8 near-signal geometry may yield a non-zero base
    hint when 1–2 gates are missing (distance-to-threshold + ATR; R8 pen_scale
    leaves room in low-ATR regimes); hurdle unchanged.
    E2A: ``KEEL_RULE_VARIANT=trend_follow`` forces 15m+1h and uses TF RSI
    bands (not-overbought / not-oversold); default ``mean_revert`` is unchanged.
    E2B (TF only): volume soft via trend+macd+ema (``soft_tf``, no RSI extreme);
    MACD lag ``KEEL_RULE_TF_MACD_LAG_BPS`` (default 3.0) for small adverse hist.
    F2a (TF only): ``KEEL_RULE_TF_MAX_EXTENSION_ATR`` (default 0=off; >0 enables)
    blocks entries already extended vs ``ema_21`` (gate ``extension_ok``).
    F2b (TF only): RSI pullback gate (``KEEL_RULE_TF_PULLBACK`` default 0;
    long ``rsi<=52``, short ``rsi>=48``; gate ``pullback_ok``).
    P2: ``KEEL_RULE_VARIANT=score`` uses diagnose ``missing==[]`` (score ≥ min
    after RSI veto / range must-haves); P1 ``regime`` range still ANDs %B gates.
    P5: ``KEEL_RULE_VARIANT=squeeze_release`` fires on squeeze expansion + ST+1h
    (same ``missing==[]`` / nearest path as score).
    """
    diag = diagnose_rule_signal(snapshot)

    if not diag["data_valid"]:
        return Decision(
            inst_id=snapshot.inst_id,
            action="WAIT",
            reason="invalid market data",
            signal_diag=diag,
        )

    if str(diag.get("rule_variant") or "") in ("regime", "score") and str(
        diag.get("regime") or ""
    ) in (
        "squeeze",
        "shock",
    ):
        return Decision(
            inst_id=snapshot.inst_id,
            action="WAIT",
            confidence=40.0,
            reason=_factor_reason(snapshot, f"no rule signal regime {diag.get('regime')}"),
            signal_diag=diag,
        )

    price = snapshot.price
    atr = snapshot.atr_14
    margin = 50.0

    variant = str(diag.get("rule_variant") or "mean_revert")
    if variant in ("supertrend", "donchian"):
        long_ok, short_ok = tv_long_short_ok(diag)
        if long_ok:
            entry = price
            sl = entry - 1.0 * atr
            tp = entry + 2.2 * atr
            return Decision(
                inst_id=snapshot.inst_id,
                action="BUY_LONG",
                confidence=70.0,
                entry_price=entry,
                take_profit=tp,
                stop_loss=sl,
                leverage=3,
                margin_usdt=margin,
                reason=_factor_reason(snapshot, f"rule long {variant}"),
                signal_diag=diag,
            )
        if short_ok:
            entry = price
            sl = entry + 1.0 * atr
            tp = entry - 2.2 * atr
            return Decision(
                inst_id=snapshot.inst_id,
                action="SELL_SHORT",
                confidence=70.0,
                entry_price=entry,
                take_profit=tp,
                stop_loss=sl,
                leverage=3,
                margin_usdt=margin,
                reason=_factor_reason(snapshot, f"rule short {variant}"),
                signal_diag=diag,
            )
        return Decision(
            inst_id=snapshot.inst_id,
            action="WAIT",
            reason=f"no rule signal {variant}{_missing_gate_suffix(diag)}",
            signal_diag=diag,
        )

    max_ext = float(diag.get("max_extension_atr") or 0.0)
    ext_long_ok, _ = _tf_extension_metrics(
        snapshot, side="long", max_extension_atr=max_ext
    )
    ext_short_ok, _ = _tf_extension_metrics(
        snapshot, side="short", max_extension_atr=max_ext
    )
    pb_enabled = bool(diag.get("tf_pullback_enabled", False))
    pb_long_max = float(diag.get("rsi_pullback_long_max") or 0.0)
    pb_short_min = float(diag.get("rsi_pullback_short_min") or 0.0)
    pb_long_ok = _tf_pullback_ok(
        snapshot,
        side="long",
        enabled=pb_enabled,
        long_max=pb_long_max,
        short_min=pb_short_min,
    )
    pb_short_ok = _tf_pullback_ok(
        snapshot,
        side="short",
        enabled=pb_enabled,
        long_max=pb_long_max,
        short_min=pb_short_min,
    )
    adx_ok_tf = bool(diag.get("adx_ok", True))
    rsi_mid_enabled = bool(diag.get("rsi_mid_enabled", False))
    rsi_mid_long_ok = _rsi_mid_ok_side(
        snapshot,
        side="long",
        enabled=rsi_mid_enabled,
        short_max=float(diag.get("short_rsi_max") or _RULE_SHORT_RSI_MAX_DEFAULT),
        long_min=float(diag.get("long_rsi_min") or _RULE_LONG_RSI_MIN_DEFAULT),
    )
    rsi_mid_short_ok = _rsi_mid_ok_side(
        snapshot,
        side="short",
        enabled=rsi_mid_enabled,
        short_max=float(diag.get("short_rsi_max") or _RULE_SHORT_RSI_MAX_DEFAULT),
        long_min=float(diag.get("long_rsi_min") or _RULE_LONG_RSI_MIN_DEFAULT),
    )
    if str(diag.get("rule_variant") or "") in ("score", "squeeze_release"):
        missing = diag.get("missing") or []
        full = isinstance(missing, list) and len(missing) == 0
        nearest = str(diag.get("nearest") or "")
        long_ok = full and nearest == "long"
        short_ok = full and nearest == "short"
    elif diag.get("rule_variant") == "regime" and str(diag.get("regime") or "") == "range":
        missing = diag.get("missing") or []
        full = isinstance(missing, list) and len(missing) == 0
        nearest = str(diag.get("nearest") or "")
        long_ok = full and nearest == "long"
        short_ok = full and nearest == "short"
    else:
        long_ok = (
            diag["rsi_long_ok"]
            and diag["trend_bullish"]
            and diag["macd_long_ok"]
            and diag["ema_long_ok"]
            and diag["volume_ok"]
            and ext_long_ok
            and pb_long_ok
            and adx_ok_tf
            and rsi_mid_long_ok
        )
        short_ok = (
            diag["rsi_short_ok"]
            and diag["trend_bearish"]
            and diag["macd_short_ok"]
            and diag["ema_short_ok"]
            and diag["volume_ok"]
            and ext_short_ok
            and pb_short_ok
            and adx_ok_tf
            and rsi_mid_short_ok
        )
    if long_ok:
        entry = price
        sl = entry - 1.0 * atr
        tp = entry + 2.2 * atr
        return Decision(
            inst_id=snapshot.inst_id,
            action="BUY_LONG",
            confidence=70.0,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            leverage=3,
            margin_usdt=margin,
            reason=_factor_reason(
                snapshot,
                _rule_reason_prefix(diag, fired=True, side="long"),
            ),
            signal_diag=diag,
        )

    if short_ok:
        entry = price
        sl = entry + 1.0 * atr
        tp = entry - 2.2 * atr
        return Decision(
            inst_id=snapshot.inst_id,
            action="SELL_SHORT",
            confidence=70.0,
            entry_price=entry,
            take_profit=tp,
            stop_loss=sl,
            leverage=3,
            margin_usdt=margin,
            reason=_factor_reason(
                snapshot,
                _rule_reason_prefix(diag, fired=True, side="short"),
            ),
            signal_diag=diag,
        )

    return Decision(
        inst_id=snapshot.inst_id,
        action="WAIT",
        confidence=40.0,
        reason=_factor_reason(
            snapshot,
            _rule_reason_prefix(diag, fired=False, side="long"),
        ),
        signal_diag=diag,
    )


class StubDecisionPolicy:
    """
    Offline / test policy: always WAIT (ignores market data).

    Useful when tests need a no-op decision path without exercising rules.
    """

    @property
    def name(self) -> str:
        return "stub"

    def decide(self, ctx: PolicyContext) -> PolicyResult:
        decisions = {
            inst_id: Decision(inst_id=inst_id, action="WAIT", reason="stub policy")
            for inst_id in ctx.instrument_ids
        }
        return PolicyResult(decisions=decisions, policy_name=self.name, success=True)


class RuleDecisionPolicy:
    """Deterministic RSI/trend/MACD/EMA/vol rules — default without LLM."""

    @property
    def name(self) -> str:
        return "rule"

    def decide(self, ctx: PolicyContext) -> PolicyResult:
        decisions: dict[str, Decision] = {}
        for inst_id in ctx.instrument_ids:
            snap = ctx.snapshots.get(inst_id)
            if snap is None:
                decisions[inst_id] = Decision(
                    inst_id=inst_id,
                    action="WAIT",
                    reason="missing snapshot",
                    valid=False,
                    validation_error="missing snapshot",
                )
                continue
            decisions[inst_id] = validate_decision(rule_based_decision(snap))
        return PolicyResult(decisions=decisions, policy_name=self.name, success=True)


# Protocol conformance (static typing helpers; runtime_checkable also works)
_STUB_CHECK: DecisionPolicy = StubDecisionPolicy()
_RULE_CHECK: DecisionPolicy = RuleDecisionPolicy()
