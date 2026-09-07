"""Q1: OKX key capability probe (mock transport; no live network / no orders)."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from keel.exchange.capability import (
    clear_capability_cache,
    is_permission_denied,
    probe_okx_capability,
)
from keel.exchange.okx_rest import OkxRestAdapter


def _okx_payload(data, code="0", msg=""):
    return json.dumps({"code": code, "msg": msg, "data": data})


def _balance_ok():
    return _okx_payload(
        [
            {
                "imr": "0",
                "details": [
                    {
                        "ccy": "USDT",
                        "eq": "1000",
                        "availBal": "1000",
                        "cashBal": "1000",
                        "upl": "0",
                    }
                ],
            }
        ]
    )


class ScriptedTransport:
    """Return scripted OKX JSON bodies by URL substring."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> str:
        self.calls.append((method, url))
        for key, payload in self.responses.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected URL: {url}")


def _settings(**kwargs):
    base = dict(
        okx_api_key="test-key",
        okx_secret_key="test-secret",
        okx_passphrase="test-pass",
        okx_environment="live",
        is_demo=False,
        okx_configured=True,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestPermissionClassifier(unittest.TestCase):
    def test_permission_keywords_and_codes(self):
        self.assertTrue(is_permission_denied("OKX API error 50110: Invalid permissions"))
        self.assertTrue(is_permission_denied("permission denied"))
        self.assertTrue(is_permission_denied(ValueError("Unauthorized request")))
        self.assertTrue(is_permission_denied("OKX API error 50113: bad"))
        self.assertFalse(is_permission_denied("OKX API error 50011: Rate limit"))
        self.assertFalse(is_permission_denied("connection timed out"))


class TestCapabilityProbe(unittest.TestCase):
    def setUp(self):
        clear_capability_cache()

    def tearDown(self):
        clear_capability_cache()

    def test_no_keys_none(self):
        s = _settings(
            okx_api_key="",
            okx_secret_key="",
            okx_passphrase="",
            okx_configured=False,
        )
        r = probe_okx_capability(s)
        self.assertEqual(r.level, "none")
        self.assertIn("not configured", r.detail)

    def test_force_paper(self):
        r = probe_okx_capability(_settings(), force_paper=True)
        self.assertEqual(r.level, "paper")

    def test_balance_ok_orders_permission_fail_read(self):
        transport = ScriptedTransport(
            {
                "/account/balance": _balance_ok(),
                "/trade/orders-pending": _okx_payload(
                    [], code="50110", msg="API key doesn't have permission"
                ),
            }
        )
        r = probe_okx_capability(_settings(), transport=transport, force_refresh=True)
        self.assertEqual(r.level, "read")
        self.assertTrue(is_permission_denied(r.detail))
        urls = [u for _, u in transport.calls]
        self.assertTrue(any("/account/balance" in u for u in urls))
        self.assertTrue(any("/trade/orders-pending" in u for u in urls))
        self.assertFalse(
            any(u.rstrip("/").endswith("/trade/order") for u in urls)
        )
        self.assertFalse(any("cancel-order" in u or "close-position" in u for u in urls))

    def test_both_ok_trade(self):
        transport = ScriptedTransport(
            {
                "/account/balance": _balance_ok(),
                "/trade/orders-pending": _okx_payload([]),
            }
        )
        r = probe_okx_capability(_settings(), transport=transport, force_refresh=True)
        self.assertEqual(r.level, "trade")
        methods_paths = list(transport.calls)
        self.assertTrue(any("/account/balance" in u for m, u in methods_paths))
        self.assertTrue(any("/trade/orders-pending" in u for m, u in methods_paths))
        self.assertTrue(all(m == "GET" for m, _ in methods_paths))

    def test_unexpected_orders_error(self):
        transport = ScriptedTransport(
            {
                "/account/balance": _balance_ok(),
                "/trade/orders-pending": _okx_payload(
                    [], code="50011", msg="Rate limit"
                ),
            }
        )
        r = probe_okx_capability(_settings(), transport=transport, force_refresh=True)
        self.assertEqual(r.level, "error")
        self.assertIn("orders-pending", r.detail)

    def test_balance_failure_error(self):
        transport = ScriptedTransport(
            {
                "/account/balance": _okx_payload([], code="50026", msg="System error"),
            }
        )
        r = probe_okx_capability(_settings(), transport=transport, force_refresh=True)
        self.assertEqual(r.level, "error")
        self.assertIn("balance", r.detail)

    def test_cache_avoids_repeat_calls(self):
        transport = ScriptedTransport(
            {
                "/account/balance": _balance_ok(),
                "/trade/orders-pending": _okx_payload([]),
            }
        )
        s = _settings()
        a = probe_okx_capability(s, transport=transport, force_refresh=True)
        self.assertEqual(a.level, "trade")
        n1 = len(transport.calls)
        b = probe_okx_capability(s, transport=transport)
        self.assertEqual(b.level, "trade")
        self.assertEqual(len(transport.calls), n1)

    def test_never_calls_mutating_endpoints_on_adapter(self):
        adapter = OkxRestAdapter(
            api_key="k",
            secret_key="s",
            passphrase="p",
            demo=False,
            transport=ScriptedTransport(
                {
                    "/account/balance": _balance_ok(),
                    "/trade/orders-pending": _okx_payload(
                        [], code="50110", msg="permission denied"
                    ),
                }
            ),
        )
        with (
            patch.object(adapter, "place_order", side_effect=AssertionError("place")),
            patch.object(adapter, "cancel_order", side_effect=AssertionError("cancel")),
            patch.object(adapter, "close_position", side_effect=AssertionError("close")),
            patch(
                "keel.exchange.capability.OkxRestAdapter.from_settings",
                return_value=adapter,
            ),
        ):
            r = probe_okx_capability(_settings(), force_refresh=True)
        self.assertEqual(r.level, "read")


if __name__ == "__main__":
    unittest.main()
