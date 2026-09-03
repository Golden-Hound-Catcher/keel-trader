"""Stage 5: OKX REST adapter with mocked HTTP (no live network)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel.config.settings import refresh_settings
from keel.exchange.factory import build_exchange, describe_exchange
from keel.exchange.okx_rest import OkxRestAdapter, OKXRestAdapter
from keel.exchange.paper import PaperExchange, PaperAdapter
from keel.exchange.protocol import ExchangeProtocol, OrderRequest
from keel.ledger import KeelLedger
from keel.worker.cycle import run_paper_cycle


def _okx_payload(data, code="0", msg=""):
    return json.dumps({"code": code, "msg": msg, "data": data})


class MockTransport:
    """Record requests and return scripted OKX JSON bodies."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.responses = responses or {}
        self.default = _okx_payload([])

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> str:
        self.calls.append((method, url, dict(headers), body))
        # Match by path substring keys
        for key, payload in self.responses.items():
            if key in url or (body and key.encode() in (body or b"")):
                return payload
        # Path-based defaults
        if "/market/ticker" in url:
            return _okx_payload(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "last": "65000",
                        "bidPx": "64990",
                        "askPx": "65010",
                        "open24h": "64000",
                        "high24h": "66000",
                        "low24h": "63000",
                        "vol24h": "1000",
                        "ts": "1700000000000",
                    }
                ]
            )
        if "/account/balance" in url:
            return _okx_payload(
                [
                    {
                        "imr": "10",
                        "details": [
                            {
                                "ccy": "USDT",
                                "eq": "10000",
                                "availBal": "9000",
                                "cashBal": "10000",
                                "upl": "0",
                            }
                        ],
                    }
                ]
            )
        if "/account/positions" in url:
            return _okx_payload(
                [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "pos": "0.01",
                        "posSide": "long",
                        "avgPx": "64000",
                        "markPx": "65000",
                        "lever": "3",
                        "upl": "10",
                        "uplRatio": "0.01",
                        "imr": "216.67",
                        "liqPx": "50000",
                    }
                ]
            )
        if "/trade/orders-pending" in url:
            return _okx_payload([])
        if "/trade/order" in url and method == "POST":
            return _okx_payload([{"ordId": "okx-1", "sCode": "0", "sMsg": ""}])
        if "/trade/cancel-order" in url:
            return _okx_payload([{"ordId": "okx-1", "sCode": "0"}])
        if "/trade/close-position" in url:
            return _okx_payload([{"ordId": "okx-close-1"}])
        return self.default


class TestOkxRestAdapterProtocol(unittest.TestCase):
    def test_alias_and_protocol(self):
        self.assertIs(OkxRestAdapter, OKXRestAdapter)
        adapter = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", demo=True, transport=MockTransport()
        )
        self.assertIsInstance(adapter, ExchangeProtocol)
        self.assertTrue(adapter.is_demo)
        self.assertTrue(adapter.credentials_configured)
        self.assertIn("okx_rest", adapter.adapter_name)


class TestOkxRestPublicAndSigned(unittest.TestCase):
    def test_public_ticker_without_keys(self):
        transport = MockTransport()
        adapter = OkxRestAdapter(demo=True, transport=transport)
        self.assertFalse(adapter.credentials_configured)
        ticker = adapter.get_ticker("BTC-USDT-SWAP")
        self.assertIsNotNone(ticker)
        self.assertEqual(ticker.last, 65000.0)
        method, url, headers, _ = transport.calls[-1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/v5/market/ticker", url)
        self.assertNotIn("OK-ACCESS-KEY", headers)

    def test_signed_balance_requires_keys(self):
        adapter = OkxRestAdapter(demo=True, transport=MockTransport())
        with self.assertRaises(ValueError) as ctx:
            adapter.get_balance()
        self.assertIn("KEEL_OKX", str(ctx.exception))

    def test_signed_balance_and_demo_header(self):
        transport = MockTransport()
        adapter = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", demo=True, transport=transport
        )
        bal = adapter.get_balance()
        self.assertEqual(bal.total_equity, 10000.0)
        self.assertEqual(bal.available_balance, 9000.0)
        _, _, headers, _ = transport.calls[-1]
        self.assertEqual(headers["OK-ACCESS-KEY"], "k")
        self.assertEqual(headers.get("x-simulated-trading"), "1")
        self.assertIn("OK-ACCESS-SIGN", headers)

    def test_live_omits_simulated_header(self):
        transport = MockTransport()
        adapter = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", demo=False, transport=transport
        )
        adapter.get_balance()
        _, _, headers, _ = transport.calls[-1]
        self.assertNotIn("x-simulated-trading", headers)

    def test_positions_place_cancel_close(self):
        transport = MockTransport()
        adapter = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", demo=True, transport=transport
        )
        positions = adapter.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].side, "long")
        self.assertEqual(positions[0].inst_id, "BTC-USDT-SWAP")

        result = adapter.place_order(
            OrderRequest(
                inst_id="BTC-USDT-SWAP",
                side="buy",
                pos_side="long",
                size=0.01,
                order_type="limit",
                price=65000.0,
            )
        )
        self.assertTrue(result.success)
        self.assertEqual(result.order_id, "okx-1")

        self.assertTrue(adapter.cancel_order("BTC-USDT-SWAP", "okx-1"))
        close = adapter.close_position("BTC-USDT-SWAP", "long")
        self.assertTrue(close.success)

    def test_api_error_code(self):
        transport = MockTransport(
            responses={
                "/account/balance": _okx_payload([], code="50111", msg="Invalid Key")
            }
        )
        adapter = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", transport=transport
        )
        with self.assertRaises(ValueError) as ctx:
            adapter.get_balance()
        self.assertIn("Invalid Key", str(ctx.exception))


class TestBuildExchangeFactory(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "KEEL_OKX_API_KEY",
                "KEEL_OKX_SECRET_KEY",
                "KEEL_OKX_PASSPHRASE",
                "KEEL_OKX_ENV",
                "OKX_DEMO_API_KEY",
                "OKX_DEMO_SECRET_KEY",
                "OKX_DEMO_PASSPHRASE",
                "OKX_API_KEY",
                "OKX_SECRET_KEY",
                "OKX_PASSPHRASE",
                "R20_OKX_ENV",
            )
        }

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        refresh_settings()

    def _clear_okx_env(self):
        for k in self._env_backup:
            os.environ.pop(k, None)

    def test_defaults_to_paper(self):
        self._clear_okx_env()
        refresh_settings()
        ex = build_exchange()
        self.assertIsInstance(ex, PaperExchange)
        self.assertEqual(describe_exchange(ex), "paper")

    def test_okx_when_keel_keys_present(self):
        self._clear_okx_env()
        os.environ["KEEL_OKX_ENV"] = "demo"
        os.environ["KEEL_OKX_API_KEY"] = "demo-key"
        os.environ["KEEL_OKX_SECRET_KEY"] = "demo-secret"
        os.environ["KEEL_OKX_PASSPHRASE"] = "demo-pass"
        refresh_settings()
        ex = build_exchange(transport=MockTransport())
        self.assertIsInstance(ex, OkxRestAdapter)
        self.assertTrue(ex.is_demo)
        self.assertIn("okx_rest", describe_exchange(ex))

    def test_force_paper_ignores_keys(self):
        self._clear_okx_env()
        os.environ["KEEL_OKX_API_KEY"] = "demo-key"
        os.environ["KEEL_OKX_SECRET_KEY"] = "demo-secret"
        os.environ["KEEL_OKX_PASSPHRASE"] = "demo-pass"
        refresh_settings()
        ex = build_exchange(force_paper=True)
        self.assertIsInstance(ex, PaperAdapter)


class TestWorkerCycleWithOkxAdapter(unittest.TestCase):
    def test_injected_okx_adapter_cycle_wait(self):
        transport = MockTransport()
        exchange = OkxRestAdapter(
            api_key="k", secret_key="s", passphrase="p", demo=True, transport=transport
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = KeelLedger(Path(tmp) / "cycle.db")
            try:
                summary = run_paper_cycle(
                    exchange=exchange,
                    ledger=ledger,
                    instrument_ids=["BTC-USDT-SWAP"],
                    force_action="WAIT",
                )
                self.assertTrue(summary["ok"])
                self.assertEqual(summary["mode"], "okx_rest")
                self.assertIn("okx_rest", summary["adapter"])
                self.assertEqual(summary["results"][0]["action"], "WAIT")
            finally:
                ledger.close()

    def test_paper_path_still_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = KeelLedger(Path(tmp) / "cycle.db")
            try:
                summary = run_paper_cycle(
                    exchange=PaperExchange(initial_balance=10_000.0),
                    ledger=ledger,
                    instrument_ids=["BTC-USDT-SWAP"],
                    force_action="BUY_LONG",
                )
                self.assertTrue(summary["ok"])
                self.assertEqual(summary["mode"], "paper")
                self.assertTrue(summary["results"][0]["success"])
            finally:
                ledger.close()


class TestSettingsExchangeMode(unittest.TestCase):
    def tearDown(self):
        for k in (
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
            "KEEL_OKX_ENV",
        ):
            os.environ.pop(k, None)
        refresh_settings()

    def test_exchange_mode_paper_and_okx(self):
        for k in (
            "KEEL_OKX_API_KEY",
            "KEEL_OKX_SECRET_KEY",
            "KEEL_OKX_PASSPHRASE",
            "KEEL_OKX_ENV",
            "OKX_DEMO_API_KEY",
            "OKX_DEMO_SECRET_KEY",
            "OKX_DEMO_PASSPHRASE",
            "OKX_API_KEY",
            "OKX_SECRET_KEY",
            "OKX_PASSPHRASE",
        ):
            os.environ.pop(k, None)
        refresh_settings()
        from keel.config import get_settings

        self.assertEqual(get_settings().exchange_mode, "paper")

        os.environ["KEEL_OKX_API_KEY"] = "k"
        os.environ["KEEL_OKX_SECRET_KEY"] = "s"
        os.environ["KEEL_OKX_PASSPHRASE"] = "p"
        os.environ["KEEL_OKX_ENV"] = "demo"
        refresh_settings()
        self.assertEqual(get_settings().exchange_mode, "okx_rest:demo")


if __name__ == "__main__":
    unittest.main()
