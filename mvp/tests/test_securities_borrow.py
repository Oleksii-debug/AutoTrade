from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.corporate_actions import EquityState
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.securities_borrow import (
    BorrowAvailabilityEvidence,
    BorrowRecallConflict,
    BorrowRecallEvidence,
    BorrowRecallResolutionEvidence,
    DurableBorrowRecallProjection,
    borrow_resource_key,
)


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "paper-borrow"
ENVIRONMENT = "PAPER"


def availability(**overrides):
    values = dict(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        locate_id="locate-1",
        provider_revision="borrow-r7",
        capacity_quantity="100",
        hard_to_borrow=True,
        observed_at="2026-09-25T05:00:30Z",
        effective_at="2026-09-25T05:00:00Z",
        expires_at="2026-09-25T05:02:00Z",
        evidence_ref="provider:borrow-snapshot-r7",
        indicative_rate="0.0125",
    )
    values.update(overrides)
    return BorrowAvailabilityEvidence(**values)


def recall(**overrides):
    values = dict(
        recall_id="recall-1",
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        provider_revision="recall-r1",
        quantity="3",
        observed_at="2026-09-25T05:01:00Z",
        effective_at="2026-09-25T05:00:45Z",
        deadline="2026-09-25T06:00:00Z",
        evidence_ref="provider:recall-r1",
    )
    values.update(overrides)
    return BorrowRecallEvidence(**values)


def resolution(**overrides):
    values = dict(
        resolution_id="resolution-1",
        recall_id="recall-1",
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        provider_revision="recall-r2",
        resolved_quantity="1",
        observed_at="2026-09-25T05:10:00Z",
        effective_at="2026-09-25T05:09:30Z",
        evidence_ref="provider:recall-r2",
    )
    values.update(overrides)
    return BorrowRecallResolutionEvidence(**values)


class SecuritiesBorrowEvidenceTests(unittest.TestCase):
    def test_resource_identity_is_scope_and_version_bound(self):
        base = borrow_resource_key(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
        )
        self.assertTrue(base.startswith("BORROW:"))
        self.assertEqual(base, availability().resource_key)
        self.assertNotEqual(
            base,
            borrow_resource_key(
                provider_id=PROVIDER_ID,
                account_id="other-account",
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
            ),
        )
        self.assertNotEqual(
            base,
            borrow_resource_key(
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=2,
            ),
        )

    def test_availability_detail_round_trip_preserves_exact_capacity_and_provenance(self):
        evidence = availability()
        restored = BorrowAvailabilityEvidence.from_resource_detail(
            evidence.resource_detail()
        )
        self.assertEqual(restored, evidence)
        self.assertEqual(restored.capacity_quantity, Decimal("100"))
        self.assertTrue(restored.hard_to_borrow)
        self.assertEqual(restored.indicative_rate, Decimal("0.0125"))

    def test_availability_rejects_ambiguous_or_non_exact_inputs(self):
        with self.assertRaises(TypeError):
            availability(capacity_quantity=100.0)
        with self.assertRaises(ValueError):
            availability(expires_at="2026-09-25T05:00:30Z")
        detail = availability().resource_detail()
        detail["capacity_semantics"] = "AVAILABLE_TO_BORROW"
        with self.assertRaisesRegex(ValueError, "TOTAL_APPROVED_CAPACITY"):
            BorrowAvailabilityEvidence.from_resource_detail(detail)


class DurableBorrowRecallProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = f"{self.temp.name}/journal.sqlite3"
        self.store = JournalStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def projection(self, store=None, **overrides):
        values = dict(
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
        )
        values.update(overrides)
        return DurableBorrowRecallProjection(store or self.store, **values)

    def test_recall_survives_restart_and_projects_existing_equity_state(self):
        projection = self.projection()
        self.assertEqual(projection.record_recall(recall()), Decimal("3"))
        self.assertEqual(projection.active_quantity, Decimal("3"))
        self.assertEqual(
            projection.active_blocking_resources,
            (availability().resource_key,),
        )

        short = EquityState.create(
            symbol="ABC",
            quantity="-5",
            total_basis="500",
            settled_cash="1000",
            currency="USD",
            borrowed_quantity="5",
        )
        projected = projection.project_equity_state(short)
        self.assertEqual(projected.recalled_quantity, Decimal("3"))

        restarted = self.projection(JournalStore(self.path))
        self.assertEqual(restarted.active_quantity, Decimal("3"))
        self.assertEqual(restarted.active_recall_ids, ("recall-1",))
        self.assertEqual(
            restarted.project_equity_state(short).recalled_quantity,
            Decimal("3"),
        )

    def test_partial_resolution_is_evidence_bound_and_idempotent(self):
        projection = self.projection()
        projection.record_recall(recall())
        self.assertEqual(
            projection.resolve_recall(resolution()),
            Decimal("2"),
        )
        self.assertEqual(
            projection.resolve_recall(resolution()),
            Decimal("2"),
        )

        restarted = self.projection(JournalStore(self.path))
        self.assertEqual(restarted.active_quantity, Decimal("2"))
        self.assertEqual(restarted.version, 2)

        self.assertEqual(
            restarted.resolve_recall(
                resolution(
                    resolution_id="resolution-2",
                    provider_revision="recall-r3",
                    resolved_quantity="2",
                    observed_at="2026-09-25T05:12:00Z",
                    effective_at="2026-09-25T05:11:30Z",
                    evidence_ref="provider:recall-r3",
                )
            ),
            Decimal("0"),
        )
        self.assertEqual(restarted.active_blocking_resources, ())

    def test_unknown_or_attempted_close_cannot_clear_recall_without_provider_resolution(self):
        projection = self.projection()
        projection.record_recall(recall())

        # No API accepts ACK/UNKNOWN/timeout as recall resolution. Restarting
        # after an ambiguous close attempt therefore retains the full obligation.
        restarted = self.projection(JournalStore(self.path))
        self.assertEqual(restarted.active_quantity, Decimal("3"))
        self.assertEqual(restarted.version, 1)

    def test_resolution_cannot_over_release_or_cross_scope(self):
        projection = self.projection()
        projection.record_recall(recall())

        with self.assertRaisesRegex(BorrowRecallConflict, "exceeds"):
            projection.resolve_recall(
                resolution(resolved_quantity="4")
            )
        with self.assertRaisesRegex(BorrowRecallConflict, "scope"):
            projection.resolve_recall(
                resolution(
                    resolution_id="other-scope",
                    account_id="other-account",
                )
            )
        self.assertEqual(projection.active_quantity, Decimal("3"))

    def test_recall_identity_conflict_fails_closed(self):
        projection = self.projection()
        projection.record_recall(recall())
        with self.assertRaisesRegex(BorrowRecallConflict, "conflicting"):
            projection.record_recall(
                recall(provider_revision="different-revision")
            )
        self.assertEqual(projection.active_quantity, Decimal("3"))

    def test_projection_rejects_provider_recall_beyond_local_borrow(self):
        projection = self.projection()
        projection.record_recall(recall(quantity="6"))
        short = EquityState.create(
            symbol="ABC",
            quantity="-5",
            total_basis="500",
            settled_cash="1000",
            currency="USD",
            borrowed_quantity="5",
        )
        with self.assertRaisesRegex(BorrowRecallConflict, "exceeds"):
            projection.project_equity_state(short)


if __name__ == "__main__":
    unittest.main()
