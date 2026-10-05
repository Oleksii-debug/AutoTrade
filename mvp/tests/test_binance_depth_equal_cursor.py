import unittest

from mvp.autotrade_mvp.binance_spot import (
    BinanceSpotDepthContinuityPolicy,
    BinanceSpotDepthCursor,
    BinanceSpotDepthRange,
)


class BinanceSpotDepthEqualCursorRegressionTests(unittest.TestCase):
    def _range(self, first: int, final: int) -> BinanceSpotDepthRange:
        return BinanceSpotDepthRange.from_diff_depth_payload(
            {
                "e": "depthUpdate",
                "s": "BTCUSDT",
                "U": first,
                "u": final,
            }
        )

    def test_fully_consumed_range_ending_at_current_cursor_is_discarded(self):
        local = BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=105)
        decision = BinanceSpotDepthContinuityPolicy.advance(
            local=local,
            event=self._range(100, 105),
        )
        self.assertEqual(decision.disposition, "DISCARD")
        self.assertIsNone(decision.next_update_id)

    def test_overlap_that_advances_past_current_cursor_remains_applicable(self):
        local = BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=105)
        decision = BinanceSpotDepthContinuityPolicy.advance(
            local=local,
            event=self._range(100, 106),
        )
        self.assertEqual(decision.disposition, "APPLY")
        self.assertEqual(decision.next_update_id, 106)


if __name__ == "__main__":
    unittest.main()
