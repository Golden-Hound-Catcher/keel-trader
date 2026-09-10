"""LLM client parsing of OpenAI/OpenRouter message shapes."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from keel.llm.client import LLMClient, _extract_message_text


_DECISION_JSON = json.dumps(
    {
        "macro_assessment": "flat",
        "decisions": {
            "BTC-USDT-SWAP": {
                "action": "WAIT",
                "confidence": 10,
                "summary_reason": "no setup",
            }
        },
    }
)


class _FakeHttp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestExtractMessageText(unittest.TestCase):
    def test_null_content_falls_back_to_reasoning(self):
        text = _extract_message_text({"content": None, "reasoning": _DECISION_JSON})
        self.assertIn("WAIT", text)

    def test_content_list_parts(self):
        text = _extract_message_text(
            {"content": [{"type": "text", "text": _DECISION_JSON}]}
        )
        self.assertIn("macro_assessment", text)

    def test_none_message(self):
        self.assertEqual(_extract_message_text(None), "")


class TestLLMClientNullContent(unittest.TestCase):
    def test_null_content_does_not_raise_strip(self):
        payload = {
            "choices": [{"message": {"content": None, "reasoning": _DECISION_JSON}}],
            "usage": {},
        }
        client = LLMClient(api_key="k", model="test-model", json_object=False)
        with patch("urllib.request.urlopen", return_value=_FakeHttp(payload)):
            resp = client.request_decisions("sys", "usr", ["BTC-USDT-SWAP"])
        self.assertTrue(resp.success, resp.error)
        self.assertEqual(resp.decisions["BTC-USDT-SWAP"].action, "WAIT")

    def test_empty_message_is_explicit_error(self):
        payload = {"choices": [{"message": {"content": None}}], "usage": {}}
        client = LLMClient(api_key="k", model="test-model", json_object=False)
        with patch("urllib.request.urlopen", return_value=_FakeHttp(payload)):
            resp = client.request_decisions("sys", "usr", ["BTC-USDT-SWAP"])
        self.assertFalse(resp.success)
        self.assertIn("empty content", resp.error)

    def test_clean_json_extracts_object_from_prose(self):
        client = LLMClient(api_key="k", model="test-model", json_object=False)
        wrapped = "sure, here you go:\n" + _DECISION_JSON + "\nthanks"
        cleaned = client._clean_json(wrapped)
        data = json.loads(cleaned)
        self.assertEqual(data["decisions"]["BTC-USDT-SWAP"]["action"], "WAIT")

    def test_content_prose_does_not_hide_reasoning_json(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "Looking at BTC {not json} first.",
                        "reasoning": _DECISION_JSON,
                    }
                }
            ],
            "usage": {},
        }
        client = LLMClient(api_key="k", model="test-model", json_object=False)
        with patch("urllib.request.urlopen", return_value=_FakeHttp(payload)):
            resp = client.request_decisions("sys", "usr", ["BTC-USDT-SWAP"])
        self.assertTrue(resp.success, resp.error)
        self.assertEqual(resp.decisions["BTC-USDT-SWAP"].action, "WAIT")

    def test_think_tags_then_json(self):
        wrapped = f"<think>plan {{skip}}</think>\n{_DECISION_JSON}"
        client = LLMClient(api_key="k", model="test-model", json_object=False)
        cleaned = client._clean_json(wrapped)
        data = json.loads(cleaned)
        self.assertEqual(data["decisions"]["BTC-USDT-SWAP"]["action"], "WAIT")

    def test_non_json_is_request_failure_not_success(self):
        payload = {
            "choices": [{"message": {"content": "not json at all"}, "finish_reason": "stop"}],
            "usage": {},
        }
        client = LLMClient(api_key="k", model="test-model", json_object=False)
        with patch("urllib.request.urlopen", return_value=_FakeHttp(payload)):
            resp = client.request_decisions("sys", "usr", ["BTC-USDT-SWAP"])
        self.assertFalse(resp.success)
        self.assertIn("JSON parse error", resp.error)
        self.assertEqual(resp.decisions, {})

    def test_openrouter_none_disables_reasoning(self):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeHttp(
                {"choices": [{"message": {"content": _DECISION_JSON}}], "usage": {}}
            )

        client = LLMClient(
            base_url="https://openrouter.ai/api/v1",
            api_key="k",
            model="test-model",
            reasoning_effort="none",
            json_object=False,
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            resp = client.request_decisions("sys", "usr", ["BTC-USDT-SWAP"])
        self.assertTrue(resp.success, resp.error)
        self.assertEqual(captured["body"]["reasoning"], {"enabled": False, "effort": "none"})
        self.assertNotIn("reasoning_effort", captured["body"])


if __name__ == "__main__":
    unittest.main()
