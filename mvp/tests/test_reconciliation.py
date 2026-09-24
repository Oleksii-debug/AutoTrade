from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.reconciliation import (
    CoverageWindow,
    ProviderExecution,
    ProviderOrder,
    ReconciliationError,
    UnknownSubmission,
    reconcile_account,
)


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)


def complete_coverage():
    return tuple(
        CoverageWindow(
            surface=surface,
            started_at=NOW - timedelta(minutes=30),
            ended_at=NOW,
            complete=True,
            cursor_exhausted=True,
            consistency_horizon_satisfied=True,
        )
        for surface in ("OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES")
    )


class ReconciliationTests(unittest.TestCase):
    def test_empty_recent_page_never_proves_absence_with_incomplete_pagination(self):
        windows = list(complete_coverage())
        windows[1] = CoverageWindow(
            surface="ORDER_HISTORY",
            started_at=NOW - timedelta(minutes=30),
            ended_at=NOW,
            complete=True,
            cursor_exhausted=False,
            consistency_horizon_satisfied=True,
        )
        result = reconcile_account(
            run_id="11111111-1111-4111-8111-111111111111",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=1,
            closing_version=1,
            unknown_submissions=(UnknownSubmission("attempt-1", "client-1"),),
            coverage_windows=windows,
        )
        self.assertEqual(result.verdict, "INCONCLUSIVE")
        self.assertTrue(result.blocks_new_risk)
        self.assertIn("KEEP_WORST_CASE_RISK:attempt-1", result.actions)
        self.assertFalse(any("PROVEN_ABSENT" in item for item in result.matched_items))

    def test_eventual_history_window_must_have_elapsed_before_proven_absent(self):
        windows = list(complete_coverage())
        windows[3] = CoverageWindow(
            surface="ACTIVITIES",
            started_at=NOW - timedelta(minutes=30),
            ended_at=NOW,
            complete=True,
            cursor_exhausted=True,
            consistency_horizon_satisfied=False,
        )
        result = reconcile_account(
            run_id="22222222-2222-4222-8222-222222222222",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=2,
            closing_version=2,
            unknown_submissions=(UnknownSubmission("attempt-lag", "client-lag"),),
            coverage_windows=windows,
        )
        self.assertEqual(result.verdict, "INCONCLUSIVE")
        self.assertIn("KEEP_WORST_CASE_RISK:attempt-lag", result.actions)

    def test_unknown_submission_can_be_matched_by_stable_client_id(self):
        result = reconcile_account(
            run_id="33333333-3333-4333-8333-333333333333",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=3,
            closing_version=3,
            unknown_submissions=(UnknownSubmission("attempt-2", "stable-client"),),
            provider_orders=(
                ProviderOrder("provider-order-9", "stable-client", "WORKING"),
            ),
            coverage_windows=complete_coverage(),
        )
        self.assertEqual(result.verdict, "BLOCKED")
        self.assertIn("submission:attempt-2", result.matched_items)
        self.assertIn(
            "RESOLVE_UNKNOWN_PRESENT:attempt-2:order:provider-order-9",
            result.actions,
        )
        self.assertNotIn("order:provider-order-9", result.unmatched_items)

    def test_full_coverage_is_required_for_proven_absence(self):
        result = reconcile_account(
            run_id="44444444-4444-4444-8444-444444444444",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=4,
            closing_version=4,
            unknown_submissions=(
                UnknownSubmission("attempt-absent", "client-absent"),
            ),
            coverage_windows=complete_coverage(),
        )
        self.assertEqual(result.verdict, "BLOCKED")
        self.assertIn(
            "submission:attempt-absent:PROVEN_ABSENT",
            result.matched_items,
        )
        self.assertIn(
            "RESOLVE_UNKNOWN_PROVEN_ABSENT:attempt-absent",
            result.actions,
        )
        self.assertIn(
            "COMMIT_RECONCILIATION_RESOLUTIONS_BEFORE_READY",
            result.actions,
        )

    def test_manual_provider_order_and_execution_block_new_risk(self):
        result = reconcile_account(
            run_id="55555555-5555-4555-8555-555555555555",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=5,
            closing_version=5,
            provider_orders=(ProviderOrder("manual-order", None, "WORKING"),),
            provider_executions=(
                ProviderExecution("manual-fill", "manual-order", None),
            ),
            coverage_windows=complete_coverage(),
        )
        self.assertEqual(result.verdict, "BLOCKED")
        self.assertTrue(result.blocks_new_risk)
        statuses = {item.status for item in result.differences}
        self.assertIn("PROVIDER_ONLY", statuses)
        self.assertIn("IMPORT_EXTERNAL_ORDER:manual-order", result.actions)
        self.assertIn("IMPORT_EXTERNAL_EXECUTION:manual-fill", result.actions)

    def test_late_fee_or_cash_difference_blocks_until_evidenced_correction(self):
        result = reconcile_account(
            run_id="66666666-6666-4666-8666-666666666666",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=6,
            closing_version=6,
            local_balances={"USD": Decimal("100")},
            provider_balances={"USD": Decimal("99.75")},
            coverage_windows=complete_coverage(),
        )
        self.assertEqual(result.verdict, "BLOCKED")
        self.assertTrue(
            any(
                item.kind == "BALANCE_VALUE_MISMATCH"
                and item.status == "VALUE_MISMATCH"
                for item in result.differences
            )
        )

    def test_non_atomic_snapshot_with_stream_gap_is_inconclusive(self):
        result = reconcile_account(
            run_id="77777777-7777-4777-8777-777777777777",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=7,
            closing_version=7,
            coverage_windows=complete_coverage(),
            snapshot_atomic=False,
            buffered_stream_gap_detected=True,
        )
        self.assertEqual(result.verdict, "INCONCLUSIVE")
        self.assertIn("REPEAT_SNAPSHOT_AFTER_STREAM_GAP", result.actions)

    def test_clean_complete_account_is_consistent(self):
        result = reconcile_account(
            run_id="88888888-8888-4888-8888-888888888888",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=8,
            closing_version=8,
            local_provider_order_ids=("o-1",),
            local_execution_ids=("x-1",),
            provider_orders=(ProviderOrder("o-1", "c-1", "FILLED"),),
            provider_executions=(ProviderExecution("x-1", "o-1", "c-1"),),
            local_balances={"USD": "100.25"},
            provider_balances={"USD": "100.25"},
            local_positions={"instrument-1": "2"},
            provider_positions={"instrument-1": "2"},
            coverage_windows=complete_coverage(),
            snapshot_atomic=True,
        )
        self.assertEqual(result.verdict, "CONSISTENT")
        self.assertFalse(result.blocks_new_risk)
        self.assertEqual(result.differences, ())

    def test_result_projects_to_canonical_reconciliation_contract(self):
        result = reconcile_account(
            run_id="99999999-9999-4999-8999-999999999999",
            provider_id="simulated",
            account_id="paper-1",
            environment="SIMULATION",
            opening_local_version=9,
            closing_version=9,
            coverage_windows=complete_coverage(),
            snapshot_atomic=True,
        )
        root = Path(__file__).resolve().parents[2]
        schema_dir = root / "contracts" / "jsonschema"
        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in schema_dir.glob("*.json")
        }
        registry = Registry().with_resources(
            [
                (schema["$id"], Resource.from_contents(schema))
                for schema in schemas.values()
            ]
        )
        persistence = schemas["persistence.schema.json"]
        validator = Draft202012Validator(
            {"$ref": f"{persistence['$id']}#/$defs/ReconciliationRun"},
            registry=registry,
            format_checker=FormatChecker(),
        )
        validator.validate(result.to_contract_dict())

    def test_duplicate_evidence_identity_is_rejected(self):
        with self.assertRaises(ReconciliationError):
            reconcile_account(
                run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                provider_id="simulated",
                account_id="paper-1",
                environment="SIMULATION",
                opening_local_version=1,
                closing_version=1,
                local_provider_order_ids=("same", "same"),
                coverage_windows=complete_coverage(),
            )


if __name__ == "__main__":
    unittest.main()
