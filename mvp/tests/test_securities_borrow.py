from dataclasses import replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from tempfile import TemporaryDirectory
import gc
import unittest
import weakref

from mvp.autotrade_mvp.corporate_actions import EquityState
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation import ResourceAvailabilityEvidence
from mvp.autotrade_mvp.securities_borrow import (
    BorrowAvailabilityEvidence,
    BorrowRecallConflict,
    BorrowRecallEvidence,
    BorrowRecallResolutionEvidence,
    DurableBorrowRecallProjection,
    _borrow_recall_aggregate_id,
    _legacy_borrow_resource_key,
    borrow_resource_key,
    provider_borrow_evidence_receipt,
)
from mvp.tests.securities_borrow_evidence_helpers import (
    EvidencedBorrowRecallProjection,
    artifact_store_for,
    bind_provider_evidence,
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
        self.assertEqual(
            base,
            borrow_resource_key(
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                provider_environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
            ),
        )

    def test_bybit_provider_environment_separates_identity_receipt_and_reconciliation(self):
        testnet = availability(
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        demo = availability(
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet.resource_key, demo.resource_key)
        self.assertEqual(testnet.resource_detail()["provider_environment"], "TESTNET")
        self.assertEqual(
            provider_borrow_evidence_receipt(testnet)["observation"]["provider_environment"],
            "TESTNET",
        )
        with self.assertRaisesRegex(ValueError, "provider_environment"):
            availability(provider_id="BYBIT", environment="PAPER")
        with self.assertRaisesRegex(ValueError, "borrow availability scope mismatch"):
            ResourceAvailabilityEvidence(
                provider_id="BYBIT",
                account_id=ACCOUNT_ID,
                environment="PAPER",
                provider_environment="DEMO",
                snapshot_id="borrow-domain-mismatch",
                query_started_at="2026-09-25T05:00:00Z",
                query_completed_at="2026-09-25T05:00:30Z",
                valid_until="2026-09-25T05:01:30Z",
                available_resources={testnet.resource_key: testnet.capacity_quantity},
                resource_details={testnet.resource_key: testnet.resource_detail()},
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

    def test_provider_evidence_subclasses_cannot_execute_financial_callbacks(self):
        calls = []

        class HostileAvailability(BorrowAvailabilityEvidence):
            def resource_detail(self):
                calls.append("resource_detail")
                raise AssertionError("caller callback executed")

        base = availability()
        hostile = HostileAvailability(
            provider_id=base.provider_id,
            account_id=base.account_id,
            environment=base.environment,
            instrument_id=base.instrument_id,
            instrument_version=base.instrument_version,
            locate_id=base.locate_id,
            provider_revision=base.provider_revision,
            capacity_quantity=base.capacity_quantity,
            hard_to_borrow=base.hard_to_borrow,
            observed_at=base.observed_at,
            effective_at=base.effective_at,
            expires_at=base.expires_at,
            evidence_ref=base.evidence_ref,
            indicative_rate=base.indicative_rate,
        )
        with self.assertRaises(TypeError):
            provider_borrow_evidence_receipt(hostile)
        self.assertEqual(calls, [])

    def test_financial_text_rejects_string_subclass_before_strip_callback(self):
        calls = []

        class HostileText(str):
            def strip(self):
                calls.append("strip")
                raise AssertionError("caller strip executed")

        with self.assertRaises(ValueError):
            availability(provider_id=HostileText(PROVIDER_ID))
        with self.assertRaises(ValueError):
            availability(
                provider_id="BYBIT",
                environment="PAPER",
                provider_environment=HostileText("TESTNET"),
            )
        self.assertEqual(calls, [])

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
        return EvidencedBorrowRecallProjection(store or self.store, **values)

    def test_provider_environment_separates_recall_journal_and_rejects_legacy_alias(self):
        testnet = self.projection(
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        demo = self.projection(
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet.resource_key, demo.resource_key)
        self.assertEqual(
            testnet.record_recall(
                recall(
                    provider_id="BYBIT",
                    environment="PAPER",
                    provider_environment="TESTNET",
                )
            ),
            Decimal("3"),
        )
        self.assertEqual(
            demo.record_recall(
                recall(
                    provider_id="BYBIT",
                    environment="PAPER",
                    provider_environment="DEMO",
                )
            ),
            Decimal("3"),
        )
        restarted_testnet = self.projection(
            JournalStore(self.path),
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        restarted_demo = self.projection(
            JournalStore(self.path),
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="DEMO",
        )
        self.assertEqual(restarted_testnet.active_quantity, Decimal("3"))
        self.assertEqual(restarted_demo.active_quantity, Decimal("3"))

        legacy_key = _legacy_borrow_resource_key(
            provider_id="BYBIT",
            account_id="legacy-account",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
        )
        legacy_aggregate = _borrow_recall_aggregate_id(legacy_key)
        payload = {"legacy": "ambiguous-provider-domain"}
        self.store.append_event(
            {
                "event_id": "legacy-borrow-recall-domain",
                "event_type": "BorrowRecallObserved",
                "aggregate_type": "securities_borrow_recall",
                "aggregate_id": legacy_aggregate,
                "aggregate_version": "1",
                "committed_at": "2026-09-25T05:00:30Z",
                "payload": payload,
                "payload_hash": payload_digest(payload),
            }
        )
        with self.assertRaisesRegex(
            BorrowRecallConflict,
            "legacy runtime-only borrow recall history",
        ):
            DurableBorrowRecallProjection(
                self.store,
                provider_id="BYBIT",
                account_id="legacy-account",
                environment="PAPER",
                provider_environment="TESTNET",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                evidence_artifact_store=artifact_store_for(self.store),
            )

    def test_projection_rejects_noncanonical_journal_store(self):
        class JournalStoreSubclass(JournalStore):
            pass

        with self.assertRaisesRegex(
            BorrowRecallConflict,
            "JournalStore authority is invalid",
        ):
            DurableBorrowRecallProjection(
                JournalStoreSubclass(self.path),
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                evidence_artifact_store=artifact_store_for(self.store),
            )

    def test_projection_binding_weakrefs_expose_no_callable_eraser(self):
        artifacts = artifact_store_for(self.store)
        projection = DurableBorrowRecallProjection(
            self.store,
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            evidence_artifact_store=artifacts,
        )
        callbacks = [
            ref.__callback__
            for ref in weakref.getweakrefs(projection)
            if ref.__callback__ is not None
        ]
        self.assertEqual(callbacks, [])
        self.assertEqual(projection.active_quantity, Decimal("0"))

    def test_projection_binding_releases_artifact_authority_on_collection(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = artifact_store_for(store)
            artifact_ref = weakref.ref(artifacts)
            projection = DurableBorrowRecallProjection(
                store,
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                evidence_artifact_store=artifacts,
            )

            del artifacts
            del projection
            gc.collect()

            self.assertIsNone(artifact_ref())

    def test_projection_rejects_store_retarget_or_method_shadow_before_read(self):
        artifacts = artifact_store_for(self.store)
        projection = DurableBorrowRecallProjection(
            self.store,
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            evidence_artifact_store=artifacts,
        )
        other = JournalStore(f"{self.temp.name}/other.sqlite3")
        object.__setattr__(projection, "store", other)
        with self.assertRaisesRegex(
            BorrowRecallConflict,
            "authority state changed",
        ):
            _ = projection.active_quantity

        clean = DurableBorrowRecallProjection(
            self.store,
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            evidence_artifact_store=artifacts,
        )
        calls = []
        object.__setattr__(
            clean,
            "_events",
            lambda: calls.append("shadow") or [],
        )
        with self.assertRaisesRegex(
            BorrowRecallConflict,
            "shadows authority methods",
        ):
            _ = clean.active_quantity
        self.assertEqual(calls, [])

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

    def test_recall_arithmetic_is_independent_of_ambient_decimal_context(self):
        cases = (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_HALF_EVEN),
            (80, ROUND_CEILING),
        )
        for precision, rounding in cases:
            with self.subTest(precision=precision, rounding=rounding):
                with TemporaryDirectory() as directory:
                    store = JournalStore(f"{directory}/journal.sqlite3")
                    projection = EvidencedBorrowRecallProjection(
                        store,
                        provider_id=PROVIDER_ID,
                        account_id=ACCOUNT_ID,
                        environment=ENVIRONMENT,
                        instrument_id=INSTRUMENT_ID,
                        instrument_version=1,
                    )
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        projection.record_recall(
                            recall(quantity="1000000000000000000000000000000")
                        )
                        projection.resolve_recall(
                            resolution(
                                resolved_quantity="999999999999999999999999999999"
                            )
                        )
                        self.assertEqual(
                            projection.remaining("recall-1"),
                            Decimal("1"),
                        )
                        self.assertEqual(projection.active_quantity, Decimal("1"))

    def test_future_recall_is_invisible_before_provider_fact_is_observed(self):
        projection = self.projection()
        projection.record_recall(recall())

        self.assertEqual(projection.active_quantity_at("2026-09-25T05:00:30Z"), Decimal("0"))
        self.assertEqual(projection.active_quantity_at("2026-09-25T05:00:50Z"), Decimal("0"))
        self.assertEqual(projection.active_quantity_at("2026-09-25T05:01:00Z"), Decimal("3"))
        self.assertEqual(
            projection.active_blocking_resources_at("2026-09-25T05:01:00Z"),
            (availability().resource_key,),
        )

        restarted = self.projection(JournalStore(self.path))
        self.assertEqual(restarted.active_quantity_at("2026-09-25T05:00:50Z"), Decimal("0"))
        self.assertEqual(restarted.active_quantity_at("2026-09-25T05:01:00Z"), Decimal("3"))

    def test_future_resolution_cannot_release_before_observation_cut_and_survives_restart(self):
        projection = self.projection()
        projection.record_recall(recall())
        projection.resolve_recall(
            resolution(
                resolved_quantity="3",
                effective_at="2026-09-25T05:09:30Z",
                observed_at="2026-09-25T05:10:00Z",
            )
        )

        self.assertEqual(projection.active_quantity_at("2026-09-25T05:02:00Z"), Decimal("3"))
        self.assertEqual(projection.active_quantity_at("2026-09-25T05:09:45Z"), Decimal("3"))
        self.assertEqual(projection.active_quantity_at("2026-09-25T05:10:00Z"), Decimal("0"))

        restarted = self.projection(JournalStore(self.path))
        self.assertEqual(restarted.active_quantity_at("2026-09-25T05:09:45Z"), Decimal("3"))
        self.assertEqual(restarted.active_quantity_at("2026-09-25T05:10:00Z"), Decimal("0"))

    def test_partial_future_resolution_chain_is_projected_at_decision_cut(self):
        projection = self.projection()
        projection.record_recall(recall(quantity="5"))
        projection.resolve_recall(
            resolution(
                resolved_quantity="2",
                effective_at="2026-09-25T05:04:00Z",
                observed_at="2026-09-25T05:05:00Z",
            )
        )
        projection.resolve_recall(
            resolution(
                resolution_id="resolution-2",
                provider_revision="recall-r3",
                resolved_quantity="3",
                effective_at="2026-09-25T05:11:00Z",
                observed_at="2026-09-25T05:12:00Z",
                evidence_ref="provider:recall-r3",
            )
        )

        self.assertEqual(projection.active_quantity_at("2026-09-25T05:04:30Z"), Decimal("5"))
        self.assertEqual(projection.active_quantity_at("2026-09-25T05:05:00Z"), Decimal("3"))
        self.assertEqual(projection.active_quantity_at("2026-09-25T05:11:30Z"), Decimal("3"))
        self.assertEqual(projection.active_quantity_at("2026-09-25T05:12:00Z"), Decimal("0"))
        self.assertEqual(
            projection.active_recall_ids_at("2026-09-25T05:05:00Z"),
            ("recall-1",),
        )
        self.assertEqual(projection.active_recall_ids_at("2026-09-25T05:12:00Z"), ())

        restarted = self.projection(JournalStore(self.path))
        self.assertEqual(restarted.active_quantity_at("2026-09-25T05:05:00Z"), Decimal("3"))
        self.assertEqual(restarted.active_quantity_at("2026-09-25T05:12:00Z"), Decimal("0"))

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


class SecuritiesBorrowArtifactBindingTests(unittest.TestCase):
    def make_projection(self, directory):
        store = JournalStore(f"{directory}/journal.sqlite3")
        artifacts = artifact_store_for(store)
        projection = DurableBorrowRecallProjection(
            store,
            provider_id=PROVIDER_ID,
            account_id=ACCOUNT_ID,
            environment=ENVIRONMENT,
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            evidence_artifact_store=artifacts,
        )
        return store, artifacts, projection

    def test_unresolvable_recall_artifact_fails_before_journal_mutation(self):
        with TemporaryDirectory() as directory:
            store, _, projection = self.make_projection(directory)
            forged = replace(
                recall(),
                evidence_ref=(
                    "artifact:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@sha256:"
                    + "0" * 64
                ),
            )
            with self.assertRaisesRegex(
                ValueError,
                "verification failed",
            ):
                projection.record_recall(forged)
            self.assertEqual(
                store.load_events(
                    "securities_borrow_recall",
                    projection.aggregate_id,
                ),
                [],
            )

    def test_same_artifact_cannot_authorize_changed_recall_or_resolution(self):
        with TemporaryDirectory() as directory:
            _, artifacts, projection = self.make_projection(directory)
            bound_recall = bind_provider_evidence(artifacts, recall())
            forged_recall = replace(bound_recall, quantity=Decimal("4"))
            with self.assertRaisesRegex(
                ValueError,
                "does not match supplied economics",
            ):
                projection.record_recall(forged_recall)

            projection.record_recall(bound_recall)
            bound_resolution = bind_provider_evidence(artifacts, resolution())
            forged_resolution = replace(
                bound_resolution,
                resolved_quantity=Decimal("2"),
            )
            with self.assertRaisesRegex(
                ValueError,
                "does not match supplied economics",
            ):
                projection.resolve_recall(forged_resolution)
            self.assertEqual(projection.active_quantity, Decimal("3"))

    def test_corrupt_recall_artifact_fails_closed_after_restart(self):
        with TemporaryDirectory() as directory:
            store, artifacts, projection = self.make_projection(directory)
            bound = bind_provider_evidence(artifacts, recall())
            projection.record_recall(bound)
            artifact_id = bound.evidence_ref[len("artifact:"):].split(
                "@sha256:",
                1,
            )[0]
            manifest = artifacts.load_manifest(artifact_id)
            digest = manifest["sha256"].removeprefix("sha256:")
            (artifacts.objects / digest[:2] / digest).write_bytes(b"corrupt")

            with self.assertRaisesRegex(ValueError, "verification failed"):
                DurableBorrowRecallProjection(
                    JournalStore(store.path),
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    evidence_artifact_store=artifact_store_for(store),
                )



if __name__ == "__main__":
    unittest.main()
