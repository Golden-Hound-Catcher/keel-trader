"""Unit tests for keel.notify port (Null / Webhook with mock transport)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from keel.config import Settings, refresh_settings
from keel.exchange.paper import PaperAdapter
from keel.ledger import KeelLedger
from keel.notify import (
    NullNotifier,
    NotifyEvent,
    WebhookNotifier,
    build_notifier,
    cycle_notify_payload,
    describe_notifier,
)
from keel.notify.webhook import build_webhook_body
from keel.worker.cycle import run_paper_cycle


class TestNullNotifier(unittest.TestCase):
    def test_null_skips(self):
        n = NullNotifier()
        self.assertEqual(n.name, "null")
        self.assertEqual(describe_notifier(n), "null")
        result = n.notify(NotifyEvent(event="ping", payload={"a": 1}))
        self.assertTrue(result.success)
        self.assertTrue(result.skipped)


class TestWebhookNotifier(unittest.TestCase):
    def test_posts_json_via_transport(self):
        calls: list[tuple] = []

        def transport(method, url, headers, body):
            calls.append((method, url, headers, body))
            return '{"ok":true}'

        n = WebhookNotifier("https://example.test/hook", transport=transport)
        result = n.notify(
            NotifyEvent(event="trader_cycle_complete", payload={"mode": "paper", "instruments": 1})
        )
        self.assertTrue(result.success)
        self.assertFalse(result.skipped)
        self.assertEqual(len(calls), 1)
        method, url, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://example.test/hook")
        self.assertIn("application/json", headers["Content-Type"])
        parsed = json.loads(body.decode("utf-8"))
        self.assertEqual(parsed["event"], "trader_cycle_complete")
        self.assertEqual(parsed["payload"]["mode"], "paper")
        self.assertEqual(parsed["payload"]["instruments"], 1)

    def test_discord_format_posts_content(self):
        calls: list[tuple] = []

        def transport(method, url, headers, body):
            calls.append((method, url, headers, body))
            return "{}"

        n = WebhookNotifier(
            "https://discord.test/hook",
            transport=transport,
            format="discord",
        )
        result = n.notify(
            NotifyEvent(
                event="trader_cycle_complete",
                payload={"text": "Keel [warn] mode=paper ok=True denies=1 errors=0"},
            )
        )
        self.assertTrue(result.success)
        parsed = json.loads(calls[0][3].decode("utf-8"))
        self.assertEqual(list(parsed.keys()), ["content"])
        self.assertIn("Keel [warn]", parsed["content"])
        self.assertNotIn("event", parsed)

    def test_discord_content_truncated(self):
        long_text = "x" * 2500
        body = build_webhook_body(
            NotifyEvent(event="e", payload={"text": long_text}),
            format="discord",
        )
        self.assertEqual(len(body["content"]), 1900)
        self.assertTrue(body["content"].endswith("..."))

    def test_transport_error_soft_fails(self):
        def transport(method, url, headers, body):
            raise ConnectionError("down")

        n = WebhookNotifier("https://example.test/hook", transport=transport)
        result = n.notify(NotifyEvent(event="x", payload={}))
        self.assertFalse(result.success)
        self.assertIn("ConnectionError", result.detail)

    def test_empty_url_skipped(self):
        n = WebhookNotifier("", transport=lambda *a: "")
        result = n.notify(NotifyEvent(event="x", payload={}))
        self.assertTrue(result.skipped)
        self.assertFalse(result.success)


class TestBuildNotifier(unittest.TestCase):
    def tearDown(self):
        for key in (
            "KEEL_NOTIFY_WEBHOOK_URL",
            "KEEL_NOTIFY_ALERTS_ONLY",
            "KEEL_NOTIFY_FORMAT",
        ):
            os.environ.pop(key, None)
        refresh_settings()

    def test_empty_env_yields_null(self):
        os.environ.pop("KEEL_NOTIFY_WEBHOOK_URL", None)
        refresh_settings()
        n = build_notifier()
        self.assertEqual(n.name, "null")

    def test_url_env_yields_webhook(self):
        os.environ["KEEL_NOTIFY_WEBHOOK_URL"] = "https://hooks.example/keel"
        refresh_settings()
        n = build_notifier()
        self.assertEqual(n.name, "webhook")
        self.assertEqual(getattr(n, "url"), "https://hooks.example/keel")

    def test_settings_object_without_env(self):
        settings = Settings(notify_webhook_url="https://from.settings/hook")
        n = build_notifier(settings)
        self.assertEqual(n.name, "webhook")

    def test_force_null(self):
        settings = Settings(notify_webhook_url="https://ignored")
        n = build_notifier(settings, force_null=True)
        self.assertEqual(n.name, "null")

    def test_discord_format_from_settings(self):
        settings = Settings(
            notify_webhook_url="https://hooks.example/d",
            notify_format="discord",
        )
        n = build_notifier(settings)
        self.assertEqual(getattr(n, "format"), "discord")


class TestCycleNotifyPayload(unittest.TestCase):
    def test_cycle_notify_payload_shape(self):
        compact = cycle_notify_payload(
            {
                "ok": True,
                "mode": "paper",
                "adapter": "paper",
                "policy": "rule",
                "policy_success": True,
                "branding": "Keel Trader",
                "instruments": 1,
                "daily_pnl": 0.0,
                "positions": 0,
                "results": [{"action": "WAIT"}],
                "ledger_db": "/tmp/x.db",
                "notifier": "null",
            }
        )
        self.assertNotIn("ledger_db", compact)
        self.assertEqual(compact["results"][0]["action"], "WAIT")
        self.assertEqual(compact["risk_denies"], 0)
        self.assertEqual(compact["error_count"], 0)
        self.assertFalse(compact["alert"])
        self.assertEqual(compact["severity"], "ok")
        self.assertIn("text", compact)
        self.assertIn("Keel [ok]", compact["text"])

    def test_payload_enrichment_from_cycle_summary(self):
        compact = cycle_notify_payload(
            {
                "ok": True,
                "mode": "paper",
                "daily_pnl": -12.5,
                "results": [],
                "cycle_summary": {
                    "risk_denies": 2,
                    "risk_deny_reasons": [
                        {"gate": "DailyLoss", "reason": "hit"},
                        {"gate": "MaxNotional", "reason": "cap"},
                    ],
                    "error_count": 0,
                    "errors": [],
                    "duration_ms": 321,
                },
            }
        )
        self.assertEqual(compact["risk_denies"], 2)
        self.assertEqual(len(compact["risk_deny_reasons"]), 2)
        self.assertEqual(compact["duration_ms"], 321)
        self.assertTrue(compact["alert"])
        self.assertEqual(compact["severity"], "warn")
        self.assertIn("denies=2", compact["text"])

    def test_payload_error_severity(self):
        compact = cycle_notify_payload(
            {
                "ok": False,
                "mode": "paper",
                "results": [],
                "error_count": 1,
                "errors": [{"inst_id": "BTC-USDT-SWAP", "error": "boom"}],
                "duration_ms": 10,
            }
        )
        self.assertTrue(compact["alert"])
        self.assertEqual(compact["severity"], "error")
        self.assertEqual(compact["error_count"], 1)

    def test_payload_caps_lists(self):
        reasons = [{"gate": f"g{i}", "reason": "r"} for i in range(30)]
        errors = [{"inst_id": f"I{i}", "error": "e"} for i in range(30)]
        compact = cycle_notify_payload(
            {
                "ok": True,
                "results": [],
                "risk_denies": 30,
                "risk_deny_reasons": reasons,
                "error_count": 30,
                "errors": errors,
            }
        )
        self.assertEqual(len(compact["risk_deny_reasons"]), 20)
        self.assertEqual(len(compact["errors"]), 20)
        self.assertEqual(compact["error_count"], 30)
        self.assertEqual(compact["severity"], "error")


class TestCycleNotifyWiring(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "notify_cycle.db"
        self.ledger = KeelLedger(self.db)
        self.exchange = PaperAdapter(initial_balance=10_000.0)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()
        for key in (
            "KEEL_NOTIFY_WEBHOOK_URL",
            "KEEL_NOTIFY_ALERTS_ONLY",
            "KEEL_NOTIFY_FORMAT",
            "KEEL_KILL_SWITCH",
        ):
            os.environ.pop(key, None)
        refresh_settings()

    def test_default_null_notifier_in_summary(self):
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="WAIT",
        )
        self.assertEqual(summary["notifier"], "null")
        self.assertTrue(summary["notify_success"])
        self.assertTrue(summary["notify_skipped"])

    def test_injected_webhook_receives_summary(self):
        transport = MagicMock(return_value="{}")
        notifier = WebhookNotifier("https://example.test/n", transport=transport)
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="WAIT",
            notifier=notifier,
        )
        self.assertEqual(summary["notifier"], "webhook")
        self.assertTrue(summary["notify_success"])
        self.assertFalse(summary["notify_skipped"])
        transport.assert_called_once()
        args = transport.call_args[0]
        self.assertEqual(args[0], "POST")
        body = json.loads(args[3].decode("utf-8"))
        self.assertEqual(body["event"], "trader_cycle_complete")
        self.assertEqual(body["payload"]["mode"], "paper")
        self.assertEqual(body["payload"]["instruments"], 1)
        self.assertIn("alert", body["payload"])
        self.assertIn("severity", body["payload"])
        self.assertIn("text", body["payload"])
        self.assertIn("duration_ms", body["payload"])

    def test_alerts_only_skips_when_not_alert(self):
        os.environ["KEEL_NOTIFY_ALERTS_ONLY"] = "1"
        refresh_settings()
        transport = MagicMock(return_value="{}")
        notifier = WebhookNotifier("https://example.test/n", transport=transport)
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="WAIT",
            notifier=notifier,
        )
        self.assertTrue(summary["notify_skipped"])
        self.assertEqual(summary["notify_detail"], "alerts_only")
        self.assertFalse(summary.get("notify_alert"))
        transport.assert_not_called()

    def test_alerts_only_posts_when_alert(self):
        """Kill-switch + BUY_LONG yields risk_denies → alert; alerts_only still POSTs."""
        os.environ["KEEL_NOTIFY_ALERTS_ONLY"] = "1"
        os.environ["KEEL_KILL_SWITCH"] = "1"
        refresh_settings()
        transport = MagicMock(return_value="{}")
        notifier = WebhookNotifier(
            "https://example.test/n",
            transport=transport,
            format="discord",
        )
        summary = run_paper_cycle(
            exchange=self.exchange,
            ledger=self.ledger,
            instrument_ids=["BTC-USDT-SWAP"],
            force_action="BUY_LONG",
            notifier=notifier,
        )
        self.assertTrue(summary.get("notify_alert"))
        self.assertFalse(summary["notify_skipped"])
        self.assertTrue(summary["notify_success"])
        transport.assert_called_once()
        body = json.loads(transport.call_args[0][3].decode("utf-8"))
        self.assertIn("content", body)
        self.assertIn("denies=", body["content"])
        os.environ.pop("KEEL_KILL_SWITCH", None)
        refresh_settings()


if __name__ == "__main__":
    unittest.main()
