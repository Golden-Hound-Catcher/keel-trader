"""
LLM-backed DecisionPolicy using modular prompts + OpenAI-compatible client.

``LLMDecisionPolicy``: LLM is the trader (legacy; not the recommended overlay).
``VetoLLMDecisionPolicy``: P4 overlay — rule fires first; LLM may only WAIT or
confirm the same side (optional shrink). Never upgrades WAIT, never flips.
"""
from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from keel.domain.decision import (
    SUPPRESS_VETO_GATE,
    Decision,
    neutralize_suppressed_fire_diag,
    validate_decision,
)
from keel.llm.client import LLMClient
from keel.llm.prompts.compose import (
    VETO_SYSTEM_PIPELINE,
    VETO_USER_PIPELINE,
    PromptComposer,
    format_market_block,
    format_rule_block,
)
from keel.policy.protocol import PolicyContext, PolicyResult
from keel.policy.stub import RuleDecisionPolicy

_FIRE_ACTIONS = frozenset({"BUY_LONG", "SELL_SHORT"})


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _annotate(decision: Decision, **fields: Any) -> Decision:
    diag = dict(decision.signal_diag or {})
    diag.update(fields)
    return replace(decision, signal_diag=diag)


def _veto_wait(rule: Decision, reason: str, **fields: Any) -> Decision:
    extra = {"llm_veto": True, "llm_confirm": False}
    extra.update(fields)
    diag = neutralize_suppressed_fire_diag(
        rule.signal_diag, gate=SUPPRESS_VETO_GATE, extra=extra
    )
    return Decision(
        inst_id=rule.inst_id,
        action="WAIT",
        confidence=min(float(rule.confidence or 0.0), 40.0),
        reason=f"{rule.reason} | {reason}".strip(" |"),
        signal_diag=diag,
    )


def apply_llm_veto(
    rule: Decision,
    llm: Decision | None,
    *,
    fail_open: bool = False,
) -> Decision:
    """
    Merge one instrument: LLM may only veto to WAIT or confirm the same side.

    Confirmed trades keep rule geometry (entry/TP/SL). Margin may shrink, never
    increase. Opposite side / missing LLM / WAIT-upgrade are discarded.
    """
    if rule.action not in _FIRE_ACTIONS:
        # Never let the model open a rule-WAIT.
        return _annotate(rule, llm_veto=False, llm_confirm=False, llm_upgrade_blocked=True)

    if llm is None:
        if fail_open:
            return _annotate(rule, llm_veto=False, llm_confirm=False, llm_unavailable=True)
        return _veto_wait(rule, "llm veto unavailable", llm_unavailable=True)

    llm_action = str(llm.action or "WAIT")
    if llm_action == "WAIT":
        why = (llm.reason or "").strip() or "llm veto"
        return _veto_wait(rule, why)

    if llm_action != rule.action:
        return _veto_wait(rule, f"llm flip {llm_action} discarded")

    margin = float(rule.margin_usdt or 0.0)
    llm_margin = float(llm.margin_usdt or 0.0)
    if llm_margin > 0.0 and margin > 0.0:
        margin = min(margin, llm_margin)
    out = replace(
        rule,
        margin_usdt=margin,
        reason=f"{rule.reason} | llm confirm".strip(),
    )
    return _annotate(out, llm_veto=False, llm_confirm=True)


class LLMDecisionPolicy:
    """
    Compose prompts from versioned modules, call LLMClient, return Decisions.

    When the LLM call fails, returns WAIT decisions with ``success=False``
    (caller / risk layer stays safe). Does not bypass risk gates.
    """

    def __init__(
        self,
        *,
        client: LLMClient | None = None,
        composer: PromptComposer | None = None,
        override_dir: Path | str | None = None,
        strategy_version: str = "keel-stage6",
    ):
        self._client = client or LLMClient()
        self._composer = composer or PromptComposer(override_dir=override_dir)
        self._strategy_version = strategy_version

    @property
    def name(self) -> str:
        return "llm"

    def decide(self, ctx: PolicyContext) -> PolicyResult:
        snaps = {i: ctx.snapshots[i] for i in ctx.instrument_ids if i in ctx.snapshots}
        variables: dict[str, Any] = {
            "strategy_version": self._strategy_version,
            "timezone": "Asia/Shanghai",
            "active_instruments": ", ".join(ctx.instrument_ids),
            "timestamp": str(ctx.timestamp or time.time()),
            "market_block": format_market_block(snaps),
            "profile_name": "keel-default",
        }
        assembled = self._composer.compose(variables=variables)
        prompt_meta = {
            "modules_used": assembled.modules_used,
            "characters": assembled.characters,
            "prompt_valid": assembled.valid,
            "prompt_errors": assembled.errors,
            "prompt_warnings": assembled.warnings,
        }

        if not assembled.ok:
            return PolicyResult(
                decisions={
                    i: Decision(
                        inst_id=i,
                        action="WAIT",
                        valid=False,
                        validation_error="prompt assembly failed",
                        reason="prompt assembly failed",
                    )
                    for i in ctx.instrument_ids
                },
                policy_name=self.name,
                prompt_meta=prompt_meta,
                error="; ".join(assembled.errors) or "prompt assembly failed",
                success=False,
            )

        response = self._client.request_decisions(
            assembled.system,
            assembled.user,
            ctx.instrument_ids,
        )
        prompt_meta["latency_ms"] = response.latency_ms
        prompt_meta["model"] = response.model

        if not response.success:
            return PolicyResult(
                decisions={
                    i: Decision(
                        inst_id=i,
                        action="WAIT",
                        valid=False,
                        validation_error=response.error or "llm failed",
                        reason="llm unavailable",
                    )
                    for i in ctx.instrument_ids
                },
                policy_name=self.name,
                macro_assessment=response.macro_assessment,
                prompt_meta=prompt_meta,
                error=response.error,
                success=False,
            )

        # Ensure every requested id has a decision
        decisions = dict(response.decisions)
        for inst_id in ctx.instrument_ids:
            if inst_id not in decisions:
                decisions[inst_id] = Decision(inst_id=inst_id, action="WAIT", reason="llm omitted")

        return PolicyResult(
            decisions=decisions,
            policy_name=self.name,
            macro_assessment=response.macro_assessment,
            prompt_meta=prompt_meta,
            success=True,
        )


class VetoLLMDecisionPolicy:
    """
    P4 overlay: RuleDecisionPolicy proposes; LLM may only veto or confirm.

    Does not call the model when every instrument is WAIT (saves tokens).
    LLM failure on a proposed fire → WAIT unless ``KEEL_LLM_VETO_FAIL_OPEN=1``.
    Geometry always comes from the rule; risk gates stay outside this port.
    """

    def __init__(
        self,
        *,
        client: LLMClient | None = None,
        composer: PromptComposer | None = None,
        inner: RuleDecisionPolicy | None = None,
        override_dir: Path | str | None = None,
        strategy_version: str = "keel-veto",
        fail_open: bool | None = None,
    ):
        self._client = client or LLMClient()
        self._composer = composer or PromptComposer(override_dir=override_dir)
        self._inner = inner or RuleDecisionPolicy()
        self._strategy_version = strategy_version
        if fail_open is None:
            self._fail_open = _env_bool("KEEL_LLM_VETO_FAIL_OPEN", False)
        else:
            self._fail_open = bool(fail_open)

    @property
    def name(self) -> str:
        return "llm_veto"

    def decide(self, ctx: PolicyContext) -> PolicyResult:
        base = self._inner.decide(ctx)
        candidates = {
            i: d
            for i, d in base.decisions.items()
            if str(d.action) in _FIRE_ACTIONS
        }
        if not candidates:
            return PolicyResult(
                decisions=dict(base.decisions),
                policy_name=self.name,
                prompt_meta={
                    "veto_overlay": True,
                    "llm_called": False,
                    "candidate_count": 0,
                },
                success=True,
            )

        snaps = {i: ctx.snapshots[i] for i in candidates if i in ctx.snapshots}
        variables: dict[str, Any] = {
            "strategy_version": self._strategy_version,
            "timezone": "Asia/Shanghai",
            "active_instruments": ", ".join(candidates.keys()),
            "timestamp": str(ctx.timestamp or time.time()),
            "market_block": format_market_block(snaps),
            "rule_block": format_rule_block(candidates),
            "profile_name": "keel-veto",
        }
        assembled = self._composer.compose(
            variables=variables,
            system_pipeline=VETO_SYSTEM_PIPELINE,
            user_pipeline=VETO_USER_PIPELINE,
        )
        prompt_meta: dict[str, Any] = {
            "veto_overlay": True,
            "llm_called": True,
            "candidate_count": len(candidates),
            "modules_used": assembled.modules_used,
            "characters": assembled.characters,
            "prompt_valid": assembled.valid,
            "prompt_errors": assembled.errors,
            "prompt_warnings": assembled.warnings,
            "fail_open": self._fail_open,
        }

        llm_map: dict[str, Decision] = {}
        response_ok = False
        if not assembled.ok:
            prompt_meta["llm_called"] = False
        else:
            response = self._client.request_decisions(
                assembled.system,
                assembled.user,
                list(candidates.keys()),
            )
            prompt_meta["latency_ms"] = response.latency_ms
            prompt_meta["model"] = response.model
            if response.success:
                llm_map = dict(response.decisions)
                response_ok = True
            else:
                prompt_meta["error"] = response.error

        merged: dict[str, Decision] = {}
        for inst_id in ctx.instrument_ids:
            rule_d = base.decisions.get(inst_id) or Decision(
                inst_id=inst_id, action="WAIT", reason="missing rule decision"
            )
            if rule_d.action not in _FIRE_ACTIONS:
                merged[inst_id] = rule_d
                continue
            llm_d = llm_map.get(inst_id) if response_ok else None
            merged[inst_id] = validate_decision(
                apply_llm_veto(rule_d, llm_d, fail_open=self._fail_open)
            )

        return PolicyResult(
            decisions=merged,
            policy_name=self.name,
            prompt_meta=prompt_meta,
            success=assembled.ok and (response_ok or self._fail_open),
            error="" if response_ok or not assembled.ok else str(
                prompt_meta.get("error") or "llm veto failed"
            ),
        )
