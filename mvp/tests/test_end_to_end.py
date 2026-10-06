import json
from decimal import Decimal, Inexact, Rounded, ROUND_CEILING, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.pipeline import SimulatedProvider, run_multi_episode, run_vertical_slice, verify_replay


class VerticalSliceTests(unittest.TestCase):
    def test_full_path_and_restart_are_idempotent(self):
        with TemporaryDirectory() as directory:
            first = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(first.status, "filled")
            self.assertEqual(first.decision, "BUY")
            self.assertEqual(first.position, 1)
            self.assertTrue(first.reconciled)
            self.assertEqual(first.evidence_count, 1)
            self.assertFalse(first.resumed)

            restarted = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertTrue(restarted.resumed)
            self.assertEqual(restarted.order_id, first.order_id)
            self.assertEqual(restarted.fill_id, first.fill_id)
            self.assertEqual(restarted.position, 1)
            self.assertEqual(restarted.cash, first.cash)
            self.assertEqual(restarted.evidence_count, 1)
            evidence_lines = (Path(directory) / "learning-evidence.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(evidence_lines), 1)
            self.assertTrue(json.loads(evidence_lines[0])["reconciled"])

    def test_risk_rejection_creates_no_fill(self):
        with TemporaryDirectory() as directory:
            result = run_vertical_slice([100, 101, 102, 103], directory, max_notional="10")
            self.assertEqual(result.status, "risk_rejected")
            self.assertIsNone(result.fill_id)
            self.assertEqual(result.position, 0)
            self.assertTrue(result.reconciled)

    def test_invalid_market_data_is_rejected(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_vertical_slice([100, float("nan")], directory)

    def test_corrupt_checkpoint_is_not_silently_accepted(self):
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint.write_text('{"schema_version": 99}', encoding="utf-8")
            with self.assertRaises(ValueError):
                run_vertical_slice([100, 101, 102], directory)

    def test_multi_episode_buy_hold_sell_and_replay(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]]
            results = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in results], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in results], ["filled", "hold", "filled"])
            self.assertEqual(results[-1].position, 0)
            self.assertEqual(results[-1].evidence_count, 3)
            replay = run_multi_episode(episodes, directory, max_abs_position="1")
            self.assertEqual(replay[-1].position, 0)
            self.assertEqual(replay[-1].evidence_count, 3)
            checkpoint = json.loads((Path(directory) / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(len(checkpoint["postings"]), 2)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 2)

    def test_multi_episode_buy_hold_sell_restart(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]]
            results = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in results], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in results], ["filled", "hold", "filled"])
            self.assertEqual(results[-1].position, 0)
            self.assertEqual(results[-1].evidence_count, 3)
            restarted = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in restarted], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in restarted], ["filled", "hold", "filled"])
            self.assertEqual(restarted[-1].position, 0)
            self.assertEqual(restarted[-1].evidence_count, 3)
            checkpoint = json.loads((Path(directory) / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(len(checkpoint["postings"]), 2)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 2)

    def test_intent_is_durable_before_simulated_execution(self):
        with TemporaryDirectory() as directory:
            with patch.object(SimulatedProvider, "execute", side_effect=RuntimeError("simulated crash")):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(len(list((Path(directory) / "order-intents").glob("*.json"))), 1)
            self.assertFalse((Path(directory) / "checkpoint.json").exists())
            recovered = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(recovered.status, "filled")
            self.assertEqual(recovered.position, 1)

    def test_multi_episode_buy_hold_sell_restart_recovery(self):
        with TemporaryDirectory() as directory:
            episodes = [[100, 101, 102, 103], [100, 100, 100], [103, 102, 101, 100]]
            results = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in results], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in results], ["filled", "hold", "filled"])
            self.assertEqual(results[-1].position, 0)
            self.assertEqual(results[-1].evidence_count, 3)
            with patch.object(SimulatedProvider, "execute", side_effect=RuntimeError("simulated crash")):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    run_multi_episode(episodes, directory)
            restarted = run_multi_episode(episodes, directory)
            self.assertEqual([item.decision for item in restarted], ["BUY", "HOLD", "SELL"])
            self.assertEqual([item.status for item in restarted], ["filled", "hold", "filled"])
            self.assertEqual(restarted[-1].position, 0)
            self.assertEqual(restarted[-1].evidence_count, 3)

    def test_missing_evidence_after_checkpoint_is_repaired(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            evidence = Path(directory) / "learning-evidence.jsonl"
            evidence.unlink()
            replay = run_vertical_slice([100, 101, 102, 103], directory)
            self.assertEqual(replay.evidence_count, 1)
            self.assertEqual(len(evidence.read_text(encoding="utf-8").splitlines()), 1)

    def test_replay_rejects_jointly_tampered_checkpoint_and_jsonl_economics(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            evidence_path = Path(directory) / "learning-evidence.jsonl"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            evidence_id = checkpoint["evidence_ids"][0]
            checkpoint["evidence_records"][evidence_id]["cash"] = "999999.99"
            checkpoint["evidence_records"][evidence_id]["equity"] = "999999.99"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            row["cash"] = "999999.99"
            row["equity"] = "999999.99"
            evidence_path.write_text(
                json.dumps(row, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "Checkpoint evidence conflicts with this episode",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_checkpoint_ledger_mismatch_is_rejected(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["postings"][0]["position_delta"] = "2"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reconcile"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_corrupt_financial_checkpoint_rejects_unbounded_decimal_text(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["postings"][0]["cash_delta"] = "1e999999"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "checkpoint cash_delta"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_corrupt_financial_checkpoint_rejects_non_text_fill_scalar(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            fill = next(iter(checkpoint["fills"].values()))
            fill["quantity"] = 1
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "checkpoint fill quantity"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restart_rejects_self_consistent_negative_fill_fee(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            fill = next(iter(checkpoint["fills"].values()))
            posting = checkpoint["postings"][0]
            quantity = Decimal(fill["quantity"])
            price = Decimal(fill["price"])
            forged_fee = Decimal("-1")
            fill["fee"] = str(forged_fee)
            posting["fee"] = str(forged_fee)
            posting["cash_delta"] = str(-(quantity * price) - forged_fee)
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "fill fee.*non-negative"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restart_rejects_noncanonical_fill_side_before_sell_semantics(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            fill = next(iter(checkpoint["fills"].values()))
            posting = checkpoint["postings"][0]
            quantity = Decimal(fill["quantity"])
            price = Decimal(fill["price"])
            fee = Decimal(fill["fee"])
            fill["side"] = "FORGED"
            posting["position_delta"] = str(-quantity)
            posting["cash_delta"] = str(quantity * price - fee)
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "side must be BUY or SELL"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restart_rejects_self_consistent_fill_that_differs_from_durable_intent(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            fill = next(iter(checkpoint["fills"].values()))
            posting = checkpoint["postings"][0]
            forged_quantity = Decimal(fill["quantity"]) + Decimal("1")
            price = Decimal(fill["price"])
            fee = Decimal(fill["fee"])
            fill["quantity"] = str(forged_quantity)
            posting["position_delta"] = str(forged_quantity)
            posting["cash_delta"] = str(-(forged_quantity * price) - fee)
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "durable order intent does not match fill",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restart_requires_durable_intent_for_each_restored_fill(self):
        with TemporaryDirectory() as directory:
            result = run_vertical_slice([100, 101, 102, 103], directory)
            intent_path = (
                Path(directory) / "order-intents" / f"{result.order_id}.json"
            )
            intent_path.unlink()

            with self.assertRaisesRegex(
                ValueError,
                "durable order intent is unavailable",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restart_rejects_fill_symbol_outside_run_scope(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            fill = next(iter(checkpoint["fills"].values()))
            fill["symbol"] = "OTHER"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "symbol does not match run scope"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restart_cross_binds_posting_fee_to_fill(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["postings"][0]["fee"] = "0"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "reconcile"):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_restart_cross_binds_fill_map_and_deterministic_fill_identity(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            checkpoint_path = Path(directory) / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            key, fill = next(iter(checkpoint["fills"].items()))
            checkpoint["fills"] = {"different-intent": fill}
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "map key does not match client_order_id",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

            checkpoint["fills"] = {key: fill}
            fill["fill_id"] = "fill-" + "0" * 20
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "fill_id does not match simulated identity",
            ):
                run_vertical_slice([100, 101, 102, 103], directory)

    def test_simulated_symbol_requires_exact_canonical_text(self):
        class HostileSymbol(str):
            def strip(self):
                raise AssertionError("hostile symbol strip executed")

        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "canonical simulated symbol"):
                run_vertical_slice(
                    [100, 101, 102, 103],
                    directory,
                    symbol=HostileSymbol("SIM"),
                )
            with self.assertRaisesRegex(ValueError, "canonical simulated symbol"):
                run_vertical_slice(
                    [100, 101, 102, 103],
                    directory,
                    symbol=" SIM ",
                )

    def test_market_data_rejects_oversized_text_before_decimal_construction(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        with patch.object(
            pipeline_module,
            "Decimal",
            side_effect=AssertionError("unbounded Decimal construction"),
        ):
            with self.assertRaisesRegex(ValueError, "finite and positive"):
                pipeline_module.handle_market_data(["1e999999"])

    def test_market_data_rejects_hostile_scalar_subclasses_without_virtual_conversion(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        class HostileStr(str):
            def __str__(self):
                raise AssertionError("hostile str conversion")

        class HostileFloat(float):
            def __str__(self):
                raise AssertionError("hostile float conversion")

        class HostileInt(int):
            def __str__(self):
                raise AssertionError("hostile int conversion")

        class HostileDecimal(Decimal):
            def __str__(self):
                raise AssertionError("hostile Decimal conversion")

        for value in (
            HostileStr("100"),
            HostileFloat(100.0),
            HostileInt(100),
            HostileDecimal("100"),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaisesRegex(
                    ValueError,
                    "exact float, str, int or Decimal",
                ):
                    pipeline_module.handle_market_data([value])

    def test_cash_limit_and_invalid_configuration(self):
        with TemporaryDirectory() as directory:
            rejected = run_vertical_slice([100, 101, 102, 103], directory, initial_cash="10")
            self.assertEqual(rejected.status, "risk_rejected")
            self.assertEqual(rejected.position, 0)
        with TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_vertical_slice([100, 101, 102, 103], directory, fee_rate="-0.1")
            with self.assertRaises(ValueError):
                run_vertical_slice(["0.000000001"], directory)

    def test_binary_float_financial_configuration_is_rejected(self):
        cases = (
            {"initial_cash": 10000.0},
            {"order_quantity": 1.0},
            {"max_abs_position": 10.0},
            {"max_notional": 5000.0},
            {"fee_rate": 0.001},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), TemporaryDirectory() as directory:
                with self.assertRaises(TypeError):
                    run_vertical_slice([100, 101, 102, 103], directory, **kwargs)

    def test_money_quantization_ignores_mutable_module_aliases(self):
        import mvp.autotrade_mvp.pipeline as pipeline_module

        expected = pipeline_module._money("1.234567895")
        with (
            patch.object(pipeline_module, "MONEY_QUANTUM", Decimal("1")),
            patch.object(
                pipeline_module,
                "round_fraction_to_quantum",
                side_effect=AssertionError("mutable rounder alias executed"),
            ),
            patch.object(
                pipeline_module,
                "as_fraction",
                side_effect=AssertionError("mutable fraction alias executed"),
            ),
            patch.object(
                pipeline_module,
                "parse_bounded_exact_decimal",
                side_effect=AssertionError("mutable parser alias executed"),
            ),
        ):
            observed = pipeline_module._money("1.234567895")

        self.assertEqual(observed, expected)
        self.assertEqual(observed, Decimal("1.23456790"))

    def test_financial_outputs_ignore_ambient_decimal_context(self):
        prices = ["100.12345678", "101.23456789", "102.34567891", "103.45678912"]
        kwargs = {
            "order_quantity": "3.14159265",
            "max_abs_position": "10",
            "max_notional": "1000",
            "fee_rate": "0.00123456",
        }
        with TemporaryDirectory() as reference_dir, TemporaryDirectory() as hostile_dir:
            reference = run_vertical_slice(prices, reference_dir, **kwargs)
            with localcontext() as context:
                context.prec = 2
                context.rounding = ROUND_CEILING
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                hostile = run_vertical_slice(prices, hostile_dir, **kwargs)

        self.assertEqual(hostile.status, reference.status)
        self.assertEqual(hostile.decision, reference.decision)
        self.assertEqual(hostile.cash, reference.cash)
        self.assertEqual(hostile.position, reference.position)
        self.assertEqual(hostile.equity, reference.equity)
        self.assertEqual(hostile.reconciled, reference.reconciled)

    def test_buy_sell_cycle_ignores_ambient_decimal_context(self):
        episodes = [
            ["100.12345678", "101.23456789", "102.34567891", "103.45678912"],
            ["103.45678912", "102.34567891", "101.23456789", "100.12345678"],
        ]
        kwargs = {
            "order_quantity": "3.14159265",
            "max_abs_position": "10",
            "max_notional": "1000",
            "fee_rate": "0.00123456",
        }
        with TemporaryDirectory() as reference_dir, TemporaryDirectory() as hostile_dir:
            reference = run_multi_episode(episodes, reference_dir, **kwargs)
            with localcontext() as context:
                context.prec = 2
                context.rounding = ROUND_CEILING
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                hostile = run_multi_episode(episodes, hostile_dir, **kwargs)

        self.assertEqual([item.decision for item in hostile], ["BUY", "SELL"])
        self.assertEqual(
            [(item.cash, item.position, item.equity, item.reconciled) for item in hostile],
            [(item.cash, item.position, item.equity, item.reconciled) for item in reference],
        )
        self.assertEqual(hostile[-1].position, reference[-1].position)

    def test_replay_verification_detects_tampered_evidence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode([[100, 101, 102, 103], [103, 102, 101, 100]], directory)
            self.assertTrue(verify_replay(directory))
            evidence_path = Path(directory) / "learning-evidence.jsonl"
            rows = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["reconciled"] = False
            evidence_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            self.assertFalse(verify_replay(directory))
            with self.assertRaises(ValueError):
                run_vertical_slice([100, 101, 102, 103], directory)


if __name__ == "__main__":
    unittest.main()
