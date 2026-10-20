from decimal import Decimal, localcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.pipeline import (
    EconomicLedger,
    handle_market_data,
    handle_portfolio,
    run_vertical_slice,
)


class HostileDecimal(Decimal):
    pass


def _durable_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


class PipelineExactNumericIngressTests(unittest.TestCase):
    def test_market_data_preserves_float_seam_but_rejects_decimal_subclasses(self):
        self.assertEqual(handle_market_data([100.125]), [Decimal("100.12500000")])
        with self.assertRaisesRegex(ValueError, "Prices must be exact"):
            handle_market_data([HostileDecimal("100.125")])

    def test_checkpoint_initial_cash_resource_bomb_fails_before_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_vertical_slice(["100", "101", "102"], root)
            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            checkpoint["initial_cash"] = "1e999999999999999999999999"
            (root / "checkpoint.json").write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )
            before = _durable_bytes(root)
            with self.assertRaisesRegex(ValueError, "Checkpoint initial cash does not match financial configuration"):
                run_vertical_slice(["100", "101", "102"], root)
            self.assertEqual(_durable_bytes(root), before)

    def test_checkpoint_posting_resource_bomb_fails_during_reconciliation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            run_vertical_slice(["100", "101", "102"], root)
            checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            checkpoint["postings"][0]["position_delta"] = "1e999999999999999999999999"
            (root / "checkpoint.json").write_text(
                json.dumps(checkpoint),
                encoding="utf-8",
            )
            before = _durable_bytes(root)
            with self.assertRaisesRegex(ValueError, "Corrupt checkpoint position_delta"):
                run_vertical_slice(["100", "101", "102"], root)
            self.assertEqual(_durable_bytes(root), before)

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
