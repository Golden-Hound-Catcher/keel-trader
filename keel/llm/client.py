"""
OpenAI-compatible LLM client for trading decisions.

Supports strict JSON schema output for reliable parsing.
"""
from __future__ import annotations

import json
import re
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
        reasoning_effort: str | None = None,
        timeout: float = 60.0,
        json_object: bool | None = None,
    ):
        settings = get_settings()
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._api_key = api_key or settings.llm_api_key
        self._model = model or settings.llm_model
        self._reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else settings.llm_reasoning_effort
        )
        self._json_object = settings.llm_json_object if json_object is None else json_object
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
            "temperature": 0.7,
        }
        if self._json_object:
            payload["response_format"] = {"type": "json_object"}
        self._apply_reasoning(payload)

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
            choice = (result.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            raw_text = _extract_message_text(message)
            usage = result.get("usage") or {}
            if not raw_text:
                return LLMResponse(
                    success=False,
                    error="LLM returned empty content (content was null; no reasoning text)",
                    latency_ms=latency,
                    model=self._model,
                    usage=usage,
                )

            content = self._clean_json(raw_text)
            if not _is_decision_payload(content):
                preview = _preview_text(raw_text)
                finish = str(choice.get("finish_reason") or "")
                extra = f" finish_reason={finish}" if finish else ""
                body = "empty body" if not (content or "").strip() else "non-JSON body"
                return LLMResponse(
                    success=False,
                    raw_content=raw_text,
                    error=f"JSON parse error: {body}{extra}"
                    + (f" preview={preview!r}" if preview else ""),
                    latency_ms=latency,
                    model=self._model,
                    usage=usage,
                )

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

    def _apply_reasoning(self, payload: dict[str, Any]) -> None:
        """Disable or set reasoning. Ling defaults to thinking if this is omitted."""
        effort = (self._reasoning_effort or "").strip().lower()
        openrouter = "openrouter.ai" in (self._base_url or "").lower()
        if effort in ("none", "off", "false", "0"):
            if openrouter:
                payload["reasoning"] = {"enabled": False, "effort": "none"}
            return
        if effort and effort != "auto":
            payload["reasoning_effort"] = effort
            if openrouter:
                payload["reasoning"] = {"effort": effort}

    def _clean_json(self, content: str) -> str:
        """Clean JSON from markdown code blocks / surrounding reasoning prose."""
        text = _THINK_TAG_RE.sub(" ", content or "")
        text = text.replace("```json", " ").replace("```JSON", " ").replace("```", " ")
        text = text.strip()
        if not text:
            return ""
        extracted = _extract_json_object(text)
        return extracted if extracted is not None else text

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
            if not isinstance(raw_decisions, dict):
                raw_decisions = {}

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


def _is_decision_payload(content: str) -> bool:
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict)


def _preview_text(text: str, limit: int = 160) -> str:
    return " ".join((text or "").split())[:limit]


_THINK_TAG_RE = re.compile(
    r"<(think|thinking|reasoning)>\s*.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def _append_text(chunks: list[str], value: Any) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text and text not in chunks:
            chunks.append(text)
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                _append_text(chunks, item)
            elif isinstance(item, dict):
                _append_text(chunks, item.get("text") or item.get("content"))
        return
    if isinstance(value, dict):
        for key in ("content", "text", "summary"):
            _append_text(chunks, value.get(key))


def _extract_json_object(text: str) -> str | None:
    """Return the first JSON object, preferring one that contains ``decisions``."""
    decoder = json.JSONDecoder()
    chosen: str | None = None
    fallback: str | None = None
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            dumped = json.dumps(obj, ensure_ascii=False)
            if "decisions" in obj:
                chosen = dumped
            elif fallback is None:
                fallback = dumped
        i = max(end, i + 1)
    return chosen if chosen is not None else fallback


def _extract_message_text(message: dict[str, Any] | None) -> str:
    """
    OpenAI-compatible assistants may set ``content`` to null and put text in
    ``reasoning`` / ``reasoning_content``, or return content as a list of parts.
    Collect every text field so thinking-only ``content`` cannot hide JSON.
    """
    if not isinstance(message, dict):
        return ""
    chunks: list[str] = []
    _append_text(chunks, message.get("content"))
    for key in ("reasoning_content", "reasoning"):
        _append_text(chunks, message.get(key))
    return "\n".join(chunks).strip()
