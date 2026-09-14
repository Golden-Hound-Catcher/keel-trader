"""
DecisionPolicy factory for Keel worker.

Default: RuleDecisionPolicy (offline / paper).
When KEEL_DECISION_POLICY=llm and LLM is configured → LLMDecisionPolicy.
When KEEL_DECISION_POLICY=llm_veto → rule proposes, LLM may only veto/confirm.
Stub always available for tests.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from keel.config import Settings, get_settings
from keel.config.settings import _env
from keel.llm.client import LLMClient
from keel.llm.prompts.compose import PromptComposer
from keel.policy.llm_policy import LLMDecisionPolicy, VetoLLMDecisionPolicy
from keel.policy.protocol import DecisionPolicy
from keel.policy.stub import RuleDecisionPolicy, StubDecisionPolicy

logger = logging.getLogger("keel.policy")

PolicyName = Literal["rule", "stub", "llm", "llm_veto"]


def build_decision_policy(
    settings: Settings | None = None,
    *,
    name: str | None = None,
    force_stub: bool = False,
    force_rule: bool = False,
    client: LLMClient | None = None,
    override_dir: Path | str | None = None,
) -> DecisionPolicy:
    """
    Select decision policy.

    Priority:
    1. force_stub → StubDecisionPolicy
    2. force_rule → RuleDecisionPolicy
    3. explicit ``name`` arg
    4. env ``KEEL_DECISION_POLICY`` (rule|stub|llm|llm_veto)
    5. if llm / llm_veto requested and configured → that policy else Rule
    """
    settings = settings or get_settings()
    if force_stub:
        policy: DecisionPolicy = StubDecisionPolicy()
        logger.info("decision policy=%s reason=force_stub", policy.name)
        return policy
    if force_rule:
        policy = RuleDecisionPolicy()
        logger.info("decision policy=%s reason=force_rule", policy.name)
        return policy

    chosen = (name or _env("KEEL_DECISION_POLICY") or "rule").strip().lower()
    if chosen == "stub":
        policy = StubDecisionPolicy()
    elif chosen in ("llm_veto", "veto", "rule_llm"):
        if not settings.llm_configured and client is None:
            policy = RuleDecisionPolicy()
            logger.info("decision policy=%s reason=llm_veto_not_configured", policy.name)
            return policy
        prompt_dir = override_dir or os.environ.get("KEEL_PROMPT_MODULES_DIR") or None
        composer = PromptComposer(override_dir=prompt_dir)
        policy = VetoLLMDecisionPolicy(
            client=client or LLMClient(),
            composer=composer,
            override_dir=prompt_dir,
        )
    elif chosen == "llm":
        if not settings.llm_configured and client is None:
            policy = RuleDecisionPolicy()
            logger.info("decision policy=%s reason=llm_not_configured", policy.name)
            return policy
        prompt_dir = override_dir or os.environ.get("KEEL_PROMPT_MODULES_DIR") or None
        composer = PromptComposer(override_dir=prompt_dir)
        policy = LLMDecisionPolicy(
            client=client or LLMClient(),
            composer=composer,
            override_dir=prompt_dir,
        )
    else:
        policy = RuleDecisionPolicy()

    logger.info("decision policy=%s", policy.name)
    return policy


def describe_policy(policy: DecisionPolicy) -> str:
    """Human-readable policy label for logs / cycle summary."""
    return str(getattr(policy, "name", type(policy).__name__))
