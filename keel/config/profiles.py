"""
Named Keel config profiles.

Set ``KEEL_PROFILE=<name>`` to apply a bundle of non-secret defaults when the
corresponding env / ``.env`` key is **unset**. Explicit env (process or
``.env``) always wins.

Profiles do **not** change trading behavior vs today's proven code defaults for
the same knobs — they only remove the need to copy dozens of lines into every
new machine's ``.env``.
"""
from __future__ import annotations

from typing import Mapping

# Proven LLM-as-trader demo (F8/F10 overlay + F9 rule shadow). Kill/shadow/near_probe
# stay off so demo OKX can place real simulated orders.
LLM_DEMO: dict[str, str] = {
    # Primary path
    "KEEL_DECISION_POLICY": "llm",
    "KEEL_RULE_SHADOW": "1",
    "KEEL_KILL_SWITCH": "0",
    "KEEL_SHADOW_MODE": "0",
    "KEEL_SHADOW_NEAR_PROBE": "0",
    # Watchlist
    "KEEL_INSTRUMENTS": "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP",
    # OpenRouter + DeepSeek (json_object off — model/path often rejects it)
    "KEEL_LLM_BASE_URL": "https://openrouter.ai/api/v1",
    "KEEL_LLM_MODEL": "deepseek/deepseek-v4-flash-0731",
    "KEEL_LLM_JSON_OBJECT": "0",
    # LLM F8/F10 edge overlay gates
    "KEEL_LLM_EDGE_OVERLAY": "1",
    "KEEL_LLM_REQUIRE_1H": "1",
    "KEEL_LLM_REQUIRE_4H": "1",
    "KEEL_LLM_4H_MODE": "soft",  # F10: soft+4h-neutral needs 15m same-dir
    "KEEL_LLM_REQUIRE_15M_ALIGN": "1",
    # P1-2: 15m same-dir (neutral blocks) when require_15m on — rule fairness
    "KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL": "1",
    "KEEL_LLM_ADX_MIN": "15",
    "KEEL_LLM_ADX_PERIOD": "14",
    "KEEL_LLM_SHORT_RSI_MAX": "52",
    "KEEL_LLM_LONG_RSI_MIN": "48",
    "KEEL_LLM_RSI_CHASE_LONG_MAX": "65",  # F10 (was 70)
    "KEEL_LLM_RSI_CHASE_SHORT_MIN": "35",  # F10 (was 30)
    "KEEL_LLM_MIN_CONFIDENCE": "65",  # F10 llm_demo only (code default still 60)
    "KEEL_LLM_NO_SCALE_IN": "1",
    # Rule F9 shadow selectivity
    "KEEL_RULE_VARIANT": "trend_follow",
    "KEEL_RULE_4H_MODE": "hard",
    "KEEL_RULE_TF_REQUIRE_4H": "1",
    "KEEL_RULE_REQUIRE_15M_ALIGN": "1",
    "KEEL_RULE_ADX_MIN": "15",
    "KEEL_RULE_SHORT_RSI_MAX": "52",
    "KEEL_RULE_LONG_RSI_MIN": "48",
}

PROFILES: dict[str, Mapping[str, str]] = {
    "llm_demo": LLM_DEMO,
    # Aliases
    "llm-demo": LLM_DEMO,
    "demo_llm": LLM_DEMO,
}


def normalize_profile_name(raw: str | None) -> str | None:
    """Return canonical profile name or None if unset/unknown."""
    name = (raw or "").strip().lower()
    if not name:
        return None
    if name in PROFILES:
        # Canonicalize aliases to llm_demo
        if name in ("llm-demo", "demo_llm"):
            return "llm_demo"
        return name
    return None


def profile_defaults(name: str | None) -> Mapping[str, str]:
    """Return defaults mapping for a profile name (empty if unknown/unset)."""
    canonical = normalize_profile_name(name)
    if canonical is None:
        return {}
    return PROFILES[canonical]
