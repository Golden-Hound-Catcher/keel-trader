#!/usr/bin/env python3
"""Print effective Keel config (no secrets) for debugging profile / .env."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("KEEL_SKIP_DOTENV", None)

from keel.config.profiles import LLM_DEMO, normalize_profile_name  # noqa: E402
from keel.config.settings import _env, get_settings, refresh_settings  # noqa: E402
from keel.policy.stub import resolve_rule_variant  # noqa: E402


def _present(key: str) -> str:
    """show | profile | default — without values for secrets."""
    if key in os.environ:
        return "env"
    from keel.config import settings as s

    if key in s._DOTENV_VALUES:
        # Distinguishing explicit .env vs profile fill is: profile keys only
        # appear when not in the raw file — approximate via profile set.
        return "dotenv"
    return "unset"


def main() -> int:
    refresh_settings()
    settings = get_settings()
    profile = settings.profile
    print(f"profile={profile or '(none)'}")
    print(f"okx_environment={settings.okx_environment}")
    print(f"okx_configured={settings.okx_configured}")
    print(f"llm_configured={settings.llm_configured}")
    print(f"llm_base_url={settings.llm_base_url}")
    print(f"llm_model={settings.llm_model}")
    print(f"llm_json_object={settings.llm_json_object}")
    print(f"decision_policy={_env('KEEL_DECISION_POLICY', 'rule') or 'rule'}")
    print(f"rule_shadow={_env('KEEL_RULE_SHADOW', '') or '(default-by-policy)'}")
    print(f"rule_variant={resolve_rule_variant()}")
    print(f"kill_switch={settings.kill_switch}")
    print(f"shadow_mode={settings.shadow_mode}")
    print(f"shadow_near_probe={settings.shadow_near_probe}")
    print(f"instruments={','.join(settings.instruments)}")
    print(f"observe_preset={settings.observe_preset}")
    print(f"cycle_interval_seconds={settings.cycle_interval_seconds}")
    print("--- llm F8 / rule F9 knobs ---")
    keys = [
        "KEEL_LLM_EDGE_OVERLAY",
        "KEEL_LLM_4H_MODE",
        "KEEL_LLM_REQUIRE_15M_ALIGN",
        "KEEL_LLM_ADX_MIN",
        "KEEL_LLM_SHORT_RSI_MAX",
        "KEEL_LLM_LONG_RSI_MIN",
        "KEEL_LLM_MIN_CONFIDENCE",
        "KEEL_RULE_4H_MODE",
        "KEEL_RULE_TF_REQUIRE_4H",
        "KEEL_RULE_REQUIRE_15M_ALIGN",
        "KEEL_RULE_ADX_MIN",
        "KEEL_RULE_SHORT_RSI_MAX",
        "KEEL_RULE_LONG_RSI_MIN",
    ]
    for key in keys:
        print(f"{key}={_env(key, '')!r} (source~{_present(key)})")
    if profile == "llm_demo":
        print(f"--- profile llm_demo defines {len(LLM_DEMO)} keys ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
