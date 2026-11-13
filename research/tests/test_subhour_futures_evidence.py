"""Standard-discovery tests for point-in-time futures journal evidence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.run_whitebit_subhour_forward_shadow import _initial, _quote, main


def _flat_bars(last_open: datetime, count: int = 200) -> list[dict]:
    result = []
    for offset in range(count):
        opened = last_open - timedelta(minutes=15 * (count - 1 - offset))
        result.append({
            "opened_at": opened,
            "closed_at": opened + timedelta(minutes=15),
            "open": "100", "high": "100", "low": "100", "close": "100",
            "volume": "1",
        })
    return result


class SubhourFuturesEvidenceTests(unittest.TestCase):
    def test_quote_binds_exact_typed_values_to_response_identity(self):
        received = datetime(2026, 11, 12, 18, 0, 3, tzinfo=UTC)
        rows = [
            SimpleNamespace(
                market=market, bid="100", ask="100.01",
                open_interest=f"{index}123", mark_price=f"100.{index}2",
                index_price=f"99.{index}8", received_at=received,
                digest="sha256:one-response",
            )
            for index, market in enumerate((
                "SOL_PERP", "BTC_PERP", "ETH_PERP", "NEAR_PERP",
            ))
        ]

        quote = _quote(SimpleNamespace(futures_markets=lambda: rows))

        self.assertEqual(set(quote["point_in_time_futures"]), {
            "SOL_PERP", "BTC_PERP", "ETH_PERP"})
        for index, market in enumerate(("SOL_PERP", "BTC_PERP", "ETH_PERP")):
            self.assertEqual(quote["point_in_time_futures"][market], {
                "open_interest": f"{index}123",
                "mark_price": f"100.{index}2",
                "index_price": f"99.{index}8",
                "received_at": received.isoformat(),
                "source_sha256": "sha256:one-response",
            })
        self.assertNotIn("NEAR_PERP", quote["point_in_time_futures"])
        self.assertNotIn("open_interest", quote)
        self.assertNotIn("mark_price", quote)
        self.assertNotIn("index_price", quote)

    def test_completed_bar_gap_does_not_carry_missing_market_evidence(self):
        boundary = datetime(2026, 11, 12, 18, tzinfo=UTC)
        now = boundary + timedelta(seconds=3)

        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = _initial(
                int(boundary.timestamp()) - 2700,
                boundary - timedelta(minutes=45),
            )
            state["point_in_time_futures"] = {
                "BTC_PERP": {"open_interest": "stale-must-not-be-carried"}}
            (root / "checkpoint.json").write_text(
                json.dumps({"state": state}), encoding="utf-8")
            current = {
                "SOL_PERP": {
                    "open_interest": "1234", "mark_price": "100.2",
                    "index_price": "100.1", "received_at": now.isoformat(),
                    "source_sha256": "same-response-digest",
                }
            }
            module = "tools.run_whitebit_subhour_forward_shadow"
            with (
                patch(f"{module}.datetime", FixedClock),
                patch(f"{module}.WhiteBitPublicClient", return_value=object()),
                patch(f"{module}._bars", return_value=_flat_bars(
                    boundary - timedelta(minutes=15))),
                patch(f"{module}.collect_market_context", return_value={
                    "decision": "ALLOW", "evidence_sha256": "news"}),
                patch(f"{module}._quote", return_value={
                    "bid": Decimal("99.99"), "ask": Decimal("100.01"),
                    "source_sha256": "same-response-digest",
                    "received_at": now.isoformat(),
                    "point_in_time_futures": current,
                }),
            ):
                outcome = main([
                    "--state-dir", str(root),
                    "--depth-database", str(root / "depth"),
                    "--stop-file", str(root / "stop"), "--once",
                ])

            self.assertEqual(outcome, 0)
            record = json.loads((root / "decisions.jsonl").read_bytes())
            self.assertEqual(record["point_in_time_futures"], current)
            self.assertNotIn("BTC_PERP", record["point_in_time_futures"])


if __name__ == "__main__":
    unittest.main()
