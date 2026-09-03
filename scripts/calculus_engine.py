"""Deprecated shim — prefer ``keel.factors.kinematics``.

Kept for legacy scripts / tests that still ``import calculus_engine``.
Honest implementations live in ``keel.factors.kinematics`` (no quantum branding).
"""
from __future__ import annotations

import warnings

from keel.factors.kinematics import (  # noqa: F401
    calculate_calculus,
    calculate_definite_integrals,
    calculate_multi_timeframe,
    calculate_path_integrals,
    calculate_price_kinematics,
    calculate_probability_theory,
    calculate_return_statistics,
    classify_integral_regime,
    classify_kinematic_regime,
    classify_path_energy_regime,
    classify_probability_regime,
    classify_regime,
    classify_return_stat_regime,
    clip_normalise,
    diff_series,
    ema_series,
    normal_cdf,
    signed_direction,
)

# Private aliases expected by older unit tests
_ema = ema_series
_diff = diff_series
_normalise = clip_normalise
_normal_cdf = normal_cdf
_sign = signed_direction

warnings.warn(
    "scripts.calculus_engine is deprecated; use keel.factors.kinematics",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "calculate_calculus",
    "calculate_definite_integrals",
    "calculate_multi_timeframe",
    "calculate_probability_theory",
    "classify_regime",
    "classify_integral_regime",
    "classify_probability_regime",
    "_ema",
    "_diff",
    "_normalise",
    "_normal_cdf",
    "_sign",
]
