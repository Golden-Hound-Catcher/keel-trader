"""Keel LLM module - OpenAI-compatible LLM integration + prompt modules."""
from keel.llm.client import LLMClient, LLMResponse, Decision, validate_decision, DECISION_SCHEMA
from keel.llm.schema import validate_decision_payload, decision_to_payload
from keel.llm.prompts import (
    AssembledPrompt,
    PromptComposer,
    PromptModule,
    format_market_block,
    render_variables,
    validate_assembled,
)

__all__ = [
    "LLMClient",
    "LLMResponse",
    "Decision",
    "validate_decision",
    "DECISION_SCHEMA",
    "validate_decision_payload",
    "decision_to_payload",
    "AssembledPrompt",
    "PromptComposer",
    "PromptModule",
    "format_market_block",
    "render_variables",
    "validate_assembled",
]
