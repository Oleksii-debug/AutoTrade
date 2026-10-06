from decimal import Decimal, localcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.pipeline import (
    EconomicLedger,
    RUN_CONFIGURATION_FILENAME,
    handle_market_data,
    handle_portfolio,
    run_vertical_slice,
)


class HostileDecimal(Decimal):
    pass


def _checkpoint(*, configuration_digest, initial_cash="10000", postings=None, fills=None):
    return {
        "schema_version": 1,
        "configuration_digest": configuration_digest,
        "symbol": "SIM",
        "initial_cash": initial_cash,
        "postings": [] if postings is None else postings,
        "fills": {} if fills is None else fills,
        "evidence_ids": [],
        "evidence_records": {},
    }


class PipelineExactNumericIngressTests(unittest.TestCase):
    def _seed_configuration(self, root: Path) -> str:
        with self.assertRaises(ValueError):
            run_vertical_slice(["not-a-price"], root)
        payload = json.loads(
            (root / RUN_CONFIGURATION_FILENAME).read_text(encoding="utf-8")
        )
        return payload["configuration_digest"]

    def test_market_data_preserves_float_seam_but_rejects_decimal_subclasses(self):
        self.assertEqual(handle_market_data([100.125]), [Decimal("100.12500000")])
        with self.assertRaisesRegex(ValueError, "Prices must be finite and positive"):
            handle_market_data([HostileDecimal("100.125")])

    def test_checkpoint_initial_cash_resource_bomb_fails_before_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration_digest = self._seed_configuration(root)
            (root / "checkpoint.json").write_text(
                json.dumps(_checkpoint(
                    configuration_digest=configuration_digest,
                    initial_cash="1e999999999999999999999999",
                )),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Corrupt checkpoint financial scalar"):
                run_vertical_slice(["100", "101", "102"], root)
            self.assertFalse((root / "journal.sqlite3").exists())
            self.assertFalse((root / "learning-evidence.jsonl").exists())
            self.assertFalse((root / "order-intents").exists())

    def test_checkpoint_posting_resource_bomb_fails_during_reconciliation(self):
        fills = {
            "intent-1": {
                "fill_id": "fill-1",
                "client_order_id": "intent-1",
                "symbol": "SIM",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "fee": "0.1",
            }
        }
        postings = [
            {
                "fill_id": "fill-1",
                "cash_delta": "-100.1",
                "position_delta": "1e999999999999999999999999",
                "fee": "0.1",
            }
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configuration_digest = self._seed_configuration(root)
            (root / "checkpoint.json").write_text(
                json.dumps(_checkpoint(
                    configuration_digest=configuration_digest,
                    postings=postings,
                    fills=fills,
                )),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                run_vertical_slice(["100", "101", "102"], root)
            self.assertFalse((root / "journal.sqlite3").exists())
            self.assertFalse((root / "learning-evidence.jsonl").exists())

    def test_ledger_and_portfolio_ignore_ambient_decimal_context(self):
        ledger = EconomicLedger(
            Decimal("1000"),
            [{"fill_id": "fill-context", "cash_delta": "0", "position_delta": "2", "fee": "0"}],
        )
        with localcontext() as context:
            context.prec = 2
            context.Emax = 9
            context.Emin = -9
            observed = handle_portfolio(ledger, Decimal("123.456789"))
        self.assertEqual(observed, Decimal("1246.91357800"))


if __name__ == "__main__":
    unittest.main()
