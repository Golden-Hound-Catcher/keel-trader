"""Contract-face sizing and clip-to-cap behaviour."""
from __future__ import annotations

import unittest

from keel.execution.sizing import SizeConstraints, lookup_instrument, size_order


def _caps(**overrides) -> SizeConstraints:
    base = dict(
        max_margin=600.0,
        max_notional=2000.0,
        max_contracts=50.0,
        available_margin=9000.0,
    )
    base.update(overrides)
    return SizeConstraints(**base)


class TestSizeOrder(unittest.TestCase):
    def test_doge_contracts_use_face_value_1000(self):
        sized = size_order(
            inst_id="DOGE-USDT-SWAP",
            requested_margin=500.0,
            leverage=3,
            entry_price=0.089,
            constraints=_caps(),
        )
        self.assertEqual(sized.error, "")
        # notional 1500 / (0.089 * 1000) ≈ 16.85 → 16 contracts, not ~16854
        self.assertEqual(sized.size, 16.0)
        self.assertLess(sized.notional, 1500.0)
        self.assertIn("DOGE", lookup_instrument("DOGE-USDT-SWAP").name)
        self.assertEqual(lookup_instrument("DOGE-USDT-SWAP").contract_value, 1000.0)

    def test_eth_oversize_clips_to_notional_and_margin_caps(self):
        sized = size_order(
            inst_id="ETH-USDT-SWAP",
            requested_margin=1000.0,
            leverage=3,
            entry_price=2470.0,
            constraints=_caps(),
        )
        self.assertEqual(sized.error, "")
        self.assertTrue(sized.clipped)
        self.assertLessEqual(sized.notional, 2000.0)
        self.assertLessEqual(sized.margin_usdt, 600.0)
        self.assertGreaterEqual(sized.size, 1.0)
        self.assertIn("max_margin", sized.clip_notes)

    def test_btc_min_lot_rejected_when_notional_cap_too_small(self):
        # 1 BTC contract ≈ 0.01 * 65000 = 650U; a 200U cap cannot open.
        sized = size_order(
            inst_id="BTC-USDT-SWAP",
            requested_margin=80.0,
            leverage=3,
            entry_price=65000.0,
            constraints=_caps(max_notional=200.0, max_margin=80.0, max_contracts=5.0),
        )
        self.assertTrue(sized.error)
        self.assertEqual(sized.size, 0.0)
        self.assertIn("最小仓", sized.error)

    def test_zero_margin_defaults_then_sizes(self):
        sized = size_order(
            inst_id="ETH-USDT-SWAP",
            requested_margin=0.0,
            leverage=3,
            entry_price=2500.0,
            constraints=_caps(),
        )
        self.assertEqual(sized.error, "")
        self.assertIn("default_margin", sized.clip_notes)
        self.assertGreaterEqual(sized.size, 1.0)


if __name__ == "__main__":
    unittest.main()
