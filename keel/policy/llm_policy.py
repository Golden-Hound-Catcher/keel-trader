"""
LLM-backed DecisionPolicy using modular prompts + OpenAI-compatible client.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from keel.llm.client import LLMClient
from keel.llm.prompts.compose import PromptComposer, format_market_block
from keel.policy.protocol import PolicyContext, PolicyResult


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
            from keel.llm.client import Decision

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
            from keel.llm.client import Decision

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
        from keel.llm.client import Decision

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
