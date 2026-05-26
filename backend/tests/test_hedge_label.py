"""Unit tests for the hedge_label classification in process_hyperliquid_whales.

Why this test exists (per CLAUDE.md rule 8): hedge_label drives a visible UI
column and downstream interpretation ("is this whale really bearish, or just
hedging?"). The thresholds are 3 named constants (CL-A) and crossing them
changes the label across documented boundaries. A regression here would
silently mis-classify positions in production.

Run with:
    python -m backend.tests.test_hedge_label

(or `python backend/tests/test_hedge_label.py` from repo root).
"""

from __future__ import annotations

import os
import sys
import unittest

# Allow running this file directly from repo root or via -m
_here = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_here)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from main import (  # noqa: E402
    process_hyperliquid_whales,
    HEDGE_FULL_THRESHOLD,
    HEDGE_PARTIAL_THRESHOLD,
    DOUBLE_BULL_SPOT_FRACTION,
)


def _build_raw(side: str, perp_size_eth: float, ueth: float = 0.0, mainnet_eth: float = 0.0):
    """Synthesize a minimal Hyperliquid clearinghouse bundle. `side` is "long"
    (positive szi) or "short" (negative szi). Sizes are in ETH."""
    addr = "0x" + "1" * 40
    szi = perp_size_eth if side == "long" else -perp_size_eth
    return {
        addr: {
            "perp": {
                "assetPositions": [{
                    "position": {
                        "coin": "ETH",
                        "szi": str(szi),
                        "entryPx": "2000",
                        "liquidationPx": "1500" if side == "long" else "2500",
                        "leverage": {"value": 5, "type": "cross"},
                        "unrealizedPnl": "0",
                    },
                }],
            },
            "spot": {
                "balances": [{"coin": "UETH", "total": str(ueth)}],
            },
            "mainnetEth": mainnet_eth,
        },
    }


class HedgeLabelTests(unittest.TestCase):
    """Test the label transitions across the constant thresholds."""

    SPOT_PX = 2000.0  # ETH spot price used to value positions in USD

    def _label(self, raw):
        out = process_hyperliquid_whales(raw, eth_spot_price=self.SPOT_PX)
        positions = out.get("positions") or []
        self.assertEqual(len(positions), 1, "test scaffolding must produce exactly one position")
        return positions[0]["hedgeLabel"], positions[0].get("hedgeRatio")

    # ── SHORT side ────────────────────────────────────────────────────────
    def test_short_fully_hedged_via_mainnet_only(self):
        # SHORT 100 ETH perp, 100 ETH on L1, 0 UETH → ratio 1.0 → FULLY_HEDGED.
        # Validates that mainnet alone (without HL UETH) can drive a full hedge.
        raw = _build_raw("short", 100, ueth=0, mainnet_eth=100)
        label, ratio = self._label(raw)
        self.assertEqual(label, "FULLY_HEDGED")
        self.assertAlmostEqual(ratio, 1.0)

    def test_short_fully_hedged_via_ueth_only(self):
        # SHORT 100 ETH perp, 100 ETH UETH, 0 mainnet → FULLY_HEDGED.
        raw = _build_raw("short", 100, ueth=100, mainnet_eth=0)
        label, _ = self._label(raw)
        self.assertEqual(label, "FULLY_HEDGED")

    def test_short_partial_hedge(self):
        # SHORT 100 ETH perp, 50 ETH total spot (50% coverage) →
        # 0.3 ≤ 0.5 < 0.8 → PARTIAL_HEDGE.
        raw = _build_raw("short", 100, ueth=20, mainnet_eth=30)
        label, ratio = self._label(raw)
        self.assertEqual(label, "PARTIAL_HEDGE")
        self.assertAlmostEqual(ratio, 0.5)

    def test_short_directional_bet_low_coverage(self):
        # SHORT 100 ETH perp, only 10 ETH spot (10% coverage) <0.3 → DIRECTIONAL_BET.
        raw = _build_raw("short", 100, ueth=0, mainnet_eth=10)
        label, _ = self._label(raw)
        self.assertEqual(label, "DIRECTIONAL_BET")

    def test_short_boundary_full_threshold(self):
        # Exactly at HEDGE_FULL_THRESHOLD → still FULLY_HEDGED (inclusive).
        # If someone flips ≥ to > in the future this test catches it.
        spot = HEDGE_FULL_THRESHOLD * 100
        raw = _build_raw("short", 100, ueth=0, mainnet_eth=spot)
        label, _ = self._label(raw)
        self.assertEqual(label, "FULLY_HEDGED")

    def test_short_boundary_partial_threshold(self):
        # Exactly at HEDGE_PARTIAL_THRESHOLD → still PARTIAL_HEDGE (inclusive).
        spot = HEDGE_PARTIAL_THRESHOLD * 100
        raw = _build_raw("short", 100, ueth=0, mainnet_eth=spot)
        label, _ = self._label(raw)
        self.assertEqual(label, "PARTIAL_HEDGE")

    # ── LONG side ─────────────────────────────────────────────────────────
    def test_long_double_bull_when_spot_high(self):
        # LONG 100 ETH perp + spot ≥ 30 ETH (30% of size) → DOUBLE_BULL (concentration).
        spot = DOUBLE_BULL_SPOT_FRACTION * 100
        raw = _build_raw("long", 100, ueth=0, mainnet_eth=spot)
        label, _ = self._label(raw)
        self.assertEqual(label, "DOUBLE_BULL")

    def test_long_directional_bet_when_spot_low(self):
        # LONG 100 ETH perp + only 5 ETH spot (5% of size, well below 30%) →
        # DIRECTIONAL_BET (pure directional, not a concentration play).
        raw = _build_raw("long", 100, ueth=2, mainnet_eth=3)
        label, _ = self._label(raw)
        self.assertEqual(label, "DIRECTIONAL_BET")

    def test_long_no_hedge_ratio_field(self):
        # hedgeRatio is only meaningful for SHORT positions (where spot offsets
        # perp short). For LONG it should stay None.
        raw = _build_raw("long", 100, ueth=50, mainnet_eth=50)
        _, ratio = self._label(raw)
        self.assertIsNone(ratio)

    # ── Edge cases ────────────────────────────────────────────────────────
    def test_skips_non_eth_positions(self):
        # Positions on BTC/SOL/etc must NOT appear in the output. We're an
        # ETH-only tracker.
        addr = "0x" + "2" * 40
        raw = {
            addr: {
                "perp": {"assetPositions": [{"position": {
                    "coin": "BTC", "szi": "1", "entryPx": "60000",
                    "liquidationPx": "55000", "leverage": {"value": 5, "type": "cross"},
                    "unrealizedPnl": "0",
                }}]},
                "spot": {"balances": []},
                "mainnetEth": 0.0,
            },
        }
        out = process_hyperliquid_whales(raw, eth_spot_price=self.SPOT_PX)
        self.assertEqual(out["positions"], [])

    def test_total_spot_eth_sums_ueth_and_mainnet(self):
        # Field totalSpotEth must equal ueth + mainnet exactly (no double counting
        # and no leakage from USDC/HYPE which are different tokens).
        raw = _build_raw("short", 100, ueth=12, mainnet_eth=34)
        out = process_hyperliquid_whales(raw, eth_spot_price=self.SPOT_PX)
        p = out["positions"][0]
        self.assertAlmostEqual(p["totalSpotEth"], 46.0)
        self.assertAlmostEqual(p["spotUethEth"], 12.0)
        self.assertAlmostEqual(p["mainnetEth"], 34.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
