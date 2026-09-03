"""
OpenAI-compatible LLM client for trading decisions.

Supports strict JSON schema output for reliable parsing.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

from keel.config import get_settings
from keel.domain.decision import Decision, validate_decision

# Re-export for existing ``from keel.llm.client import Decision, validate_decision``
__all__ = [
    "Decision",
    "validate_decision",
    "LLMResponse",
    "LLMClient",
    "DECISION_SCHEMA",
]


@dataclass
class LLMResponse:
    """Response from LLM including decisions and metadata."""
    success: bool
    raw_content: str = ""
    decisions: dict[str, Decision] = field(default_factory=dict)
    macro_assessment: str = ""
    error: str = ""
    latency_ms: int = 0
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)


# Decision JSON schema for strict output
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "macro_assessment": {"type": "string"},
        "decisions": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["BUY_LONG", "SELL_SHORT", "WAIT"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                    "leverage": {"type": "integer", "minimum": 1, "maximum": 10},
                    "margin_usdt": {"type": "number", "minimum": 0},
                    "entry_price": {"type": "number"},
                    "take_profit_price": {"type": "number"},
                    "stop_loss_price": {"type": "number"},
                    "summary_reason": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["decisions"],
}


class LLMClient:
    """
    OpenAI-compatible LLM client.
    
    Sends trading prompts and parses structured decisions.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str = "high",
        timeout: float = 60.0,
    ):
        settings = get_settings()
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._api_key = api_key or settings.llm_api_key
        self._model = model or settings.llm_model
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout

    def request_decisions(
        self,
        system_prompt: str,
        user_prompt: str,
        instrument_ids: list[str],
    ) -> LLMResponse:
        """
        Request trading decisions from the LLM.
        
        Args:
            system_prompt: System prompt with trading rules
            user_prompt: User prompt with market data
            instrument_ids: List of instrument IDs to get decisions for
            
        Returns:
            LLMResponse with parsed decisions
        """
        import time
        start = time.time()

        if not self._api_key:
            return LLMResponse(success=False, error="LLM API key not configured")

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        if self._reasoning_effort not in ("none", "auto"):
            payload["reasoning_effort"] = self._reasoning_effort

        try:
            req = urllib.request.Request(
                f"{self._base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Keel-Trader/0.1",
                },
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            latency = int((time.time() - start) * 1000)
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = result.get("usage", {})

            content = self._clean_json(content)
            decisions = self._parse_decisions(content, instrument_ids)

            return LLMResponse(
                success=True,
                raw_content=content,
                decisions=decisions,
                macro_assessment=self._extract_macro(content),
                latency_ms=latency,
                model=self._model,
                usage=usage,
            )

        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            return LLMResponse(
                success=False,
                error=f"HTTP {e.code}: {error_body[:500]}",
                latency_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return LLMResponse(
                success=False,
                error=str(e),
                latency_ms=int((time.time() - start) * 1000),
            )

    def _clean_json(self, content: str) -> str:
        """Clean JSON from markdown code blocks."""
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        return content.strip()

    def _extract_macro(self, content: str) -> str:
        """Extract macro assessment from response."""
        try:
            data = json.loads(content)
            return str(data.get("macro_assessment", ""))[:200]
        except Exception:
            return ""

    def _parse_decisions(
        self,
        content: str,
        instrument_ids: list[str],
    ) -> dict[str, Decision]:
        """Parse decisions from LLM response."""
        decisions: dict[str, Decision] = {}

        try:
            data = json.loads(content)
            raw_decisions = data.get("decisions", {})

            for inst_id in instrument_ids:
                raw = raw_decisions.get(inst_id, {})
                if not isinstance(raw, dict):
                    raw = {}

                action = str(raw.get("action", "WAIT")).upper()
                if action not in ("BUY_LONG", "SELL_SHORT", "WAIT"):
                    action = "WAIT"

                decision = Decision(
                    inst_id=inst_id,
                    action=action,  # type: ignore
                    confidence=self._safe_float(raw.get("confidence"), 0.0, 0.0, 100.0),
                    entry_price=self._safe_float(raw.get("entry_price")),
                    take_profit=self._safe_float(raw.get("take_profit_price")),
                    stop_loss=self._safe_float(raw.get("stop_loss_price")),
                    leverage=min(10, max(1, int(raw.get("leverage", 3) or 3))),
                    margin_usdt=self._safe_float(raw.get("margin_usdt"), 0.0),
                    reason=str(raw.get("summary_reason", ""))[:200],
                )

                decision = self._validate_decision(decision)
                decisions[inst_id] = decision

        except json.JSONDecodeError as e:
            for inst_id in instrument_ids:
                decisions[inst_id] = Decision(
                    inst_id=inst_id,
                    action="WAIT",
                    valid=False,
                    validation_error=f"JSON parse error: {e}",
                )

        return decisions

    def _validate_decision(self, decision: Decision) -> Decision:
        """Validate a decision meets risk requirements."""
        return validate_decision(decision)

    def _safe_float(
        self,
        value: Any,
        default: float | None = None,
        min_val: float | None = None,
        max_val: float | None = None,
    ) -> float | None:
        """Safely convert to float with optional bounds."""
        try:
            result = float(value)
            if min_val is not None:
                result = max(result, min_val)
            if max_val is not None:
                result = min(result, max_val)
            return result
        except (TypeError, ValueError):
            return default
