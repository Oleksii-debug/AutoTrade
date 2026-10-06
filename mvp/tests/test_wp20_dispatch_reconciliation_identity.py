from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation_journal import (
    load_account_resource_availability_evidence,
    load_latest_reconciliation_checkpoint,
    load_latest_reconciliation_checkpoint_for_scope,
    load_submission_resolution_evidence,
    record_reconciliation_checkpoint,
    unknown_submissions_from_dispatch,
)


class DispatchReconciliationAttemptIdentityTests(unittest.TestCase):
    def _make_modern_unknown(
        self,
        path: Path,
        *,
        attempt_id: str = "attempt-b",
    ) -> tuple[JournalStore, str]:
        store = JournalStore(path)
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )

        def ambiguous_transport(_client_order_id, _request, final_guard):
            final_guard()
            raise RuntimeError("ambiguous after send barrier")

        result = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-b",
            intent_hash="sha256:" + "1" * 64,
            provider="TEST_PROVIDER",
            request={"side": "BUY"},
            now="2026-10-06T14:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=ambiguous_transport,
        )
        self.assertEqual(result.status, "UNKNOWN")
        aggregate_id = submission_attempt_aggregate_id(
            environment="SIMULATION",
            account_id="acct",
            attempt_id=attempt_id,
        )
        return store, aggregate_id

    @staticmethod
    def _append_legacy_unknown(store: JournalStore, aggregate_id: str) -> None:
        prepared = {
            "intent_id": "legacy-intent",
            "provider": "TEST_PROVIDER",
            "account_id": "acct",
            "environment": "SIMULATION",
            "client_order_id": "legacy-client",
            "prepared_at": "2026-10-06T14:00:00Z",
        }
        unknown = {
            "client_order_id": "legacy-client",
            "reason": "transport_result_ambiguous",
        }
        for version, event_type, payload in (
            (1, "SubmissionPrepared", prepared),
            (2, "SubmissionUnknown", unknown),
        ):
            store.append_event(
                {
                    "event_id": f"legacy-{version}",
                    "event_type": event_type,
                    "schema_version": "1.0.0",
                    "aggregate_type": "submission_attempt",
                    "aggregate_id": aggregate_id,
                    "aggregate_version": str(version),
                    "host_id": "local-mvp",
                    "owner_epoch": "1",
                    "environment": "SIMULATION",
                    "occurred_at": "2026-10-06T14:00:00Z",
                    "observed_at": "2026-10-06T14:00:00Z",
                    "committed_at": "2026-10-06T14:00:00Z",
                    "correlation_id": "legacy-correlation",
                    "causation_id": None,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "evidence_refs": [],
                }
            )

    def test_explicit_aggregate_mapping_cannot_relabel_modern_attempt(self):
        with TemporaryDirectory() as directory:
            store, aggregate_id = self._make_modern_unknown(
                Path(directory) / "journal.sqlite3"
            )

            with self.assertRaisesRegex(
                ValueError,
                "durable attempt_id does not match requested attempt identity",
            ):
                unknown_submissions_from_dispatch(
                    store,
                    attempt_ids=("attempt-a",),
                    aggregate_ids={"attempt-a": aggregate_id},
                )

            recovered = unknown_submissions_from_dispatch(
                store,
                attempt_ids=("attempt-b",),
                aggregate_ids={"attempt-b": aggregate_id},
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].attempt_id, "attempt-b")
            self.assertEqual(recovered[0].intent_id, "intent-b")

    def test_scoped_lookup_recovers_modern_attempt_without_caller_mapping(self):
        with TemporaryDirectory() as directory:
            store, _aggregate_id = self._make_modern_unknown(
                Path(directory) / "journal.sqlite3"
            )

            recovered = unknown_submissions_from_dispatch(
                store,
                attempt_ids=("attempt-b",),
                environment="SIMULATION",
                account_id="acct",
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].attempt_id, "attempt-b")
            self.assertEqual(recovered[0].account_id, "acct")
            self.assertEqual(recovered[0].environment, "SIMULATION")

    def test_legacy_original_aggregate_identity_remains_readable(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            aggregate_id = "legacy-attempt"
            self._append_legacy_unknown(store, aggregate_id)

            recovered = unknown_submissions_from_dispatch(
                store,
                attempt_ids=(aggregate_id,),
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].attempt_id, aggregate_id)
            self.assertEqual(recovered[0].intent_id, "legacy-intent")

    def test_legacy_aggregate_without_attempt_identity_cannot_be_remapped(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            aggregate_id = "submission-attempt:legacy-unbound"
            self._append_legacy_unknown(store, aggregate_id)

            with self.assertRaisesRegex(
                ValueError,
                "legacy SubmissionPrepared without durable attempt_id cannot be remapped",
            ):
                unknown_submissions_from_dispatch(
                    store,
                    attempt_ids=("caller-alias",),
                    aggregate_ids={"caller-alias": aggregate_id},
                )

    def test_journal_store_subclass_cannot_supply_reconciliation_truth(self):
        class ForgedStore(JournalStore):
            def load_events(self, _aggregate_type, _aggregate_id):
                raise AssertionError("subclass load_events must not execute")

        with TemporaryDirectory() as directory:
            forged = ForgedStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                unknown_submissions_from_dispatch(
                    forged,
                    attempt_ids=("attempt-a",),
                )

    def test_reconciliation_journal_authority_boundaries_reject_store_subclass(self):
        class ForgedStore(JournalStore):
            def load_events(self, _aggregate_type, _aggregate_id):
                raise AssertionError("subclass journal read must not execute")

            def append_event(self, _envelope, **_kwargs):
                raise AssertionError("subclass journal write must not execute")

        with TemporaryDirectory() as directory:
            forged = ForgedStore(Path(directory) / "journal.sqlite3")
            cases = (
                lambda: record_reconciliation_checkpoint(
                    forged,
                    reconciliation_id="r",
                    result=None,
                    observed_at="not-reached",
                    host_id="not-reached",
                    owner_epoch="not-reached",
                ),
                lambda: load_latest_reconciliation_checkpoint(
                    forged,
                    reconciliation_id="r",
                    provider_id="P",
                    account_id="a",
                    environment="SIMULATION",
                ),
                lambda: load_latest_reconciliation_checkpoint_for_scope(
                    forged,
                    provider_id="P",
                    account_id="a",
                    environment="SIMULATION",
                ),
                lambda: load_submission_resolution_evidence(
                    forged,
                    checkpoint_event_id="e",
                    provider_id="P",
                    account_id="a",
                    environment="SIMULATION",
                    attempt_id="attempt",
                    intent_id="intent",
                    client_order_id="client",
                ),
                lambda: load_account_resource_availability_evidence(
                    forged,
                    checkpoint_event_id="e",
                    provider_id="P",
                    account_id="a",
                    environment="SIMULATION",
                    resources=("CASH:USD",),
                    now="2026-10-06T14:00:00Z",
                    max_age_seconds="60",
                ),
            )
            for boundary in cases:
                with self.subTest(boundary=boundary), self.assertRaisesRegex(
                    TypeError,
                    "exact JournalStore",
                ):
                    boundary()

    def test_instance_shadow_cannot_supply_reconciliation_truth(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")

            def forged_load_events(_aggregate_type, _aggregate_id):
                return [
                    {
                        "event_type": "SubmissionPrepared",
                        "environment": "SIMULATION",
                        "payload": {
                            "attempt_id": "attempt-a",
                            "intent_id": "forged-intent",
                            "provider": "TEST_PROVIDER",
                            "account_id": "acct",
                            "environment": "SIMULATION",
                            "client_order_id": "forged-client",
                            "prepared_at": "2026-10-06T14:00:00Z",
                        },
                    },
                    {
                        "event_type": "SubmissionUnknown",
                        "environment": "SIMULATION",
                        "payload": {
                            "client_order_id": "forged-client",
                            "reason": "forged",
                        },
                    },
                ]

            store.load_events = forged_load_events
            with self.assertRaisesRegex(TypeError, "shadowed"):
                unknown_submissions_from_dispatch(
                    store,
                    attempt_ids=("attempt-a",),
                )


    def test_dispatch_recovery_rejects_polymorphic_text_without_executing_it(self):
        class TrapText(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("caller-controlled strip executed")

            def upper(self):
                raise AssertionError("caller-controlled upper executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            for kwargs, expected in (
                (
                    {"attempt_ids": (TrapText("attempt-a"),)},
                    "attempt_id is required",
                ),
                (
                    {
                        "attempt_ids": ("attempt-a",),
                        "aggregate_ids": {
                            TrapText("attempt-a"): "submission-attempt:a"
                        },
                    },
                    "aggregate_ids attempt_id is required",
                ),
                (
                    {
                        "attempt_ids": ("attempt-a",),
                        "aggregate_ids": {
                            "attempt-a": TrapText("submission-attempt:a")
                        },
                    },
                    "aggregate_id is required",
                ),
                (
                    {
                        "attempt_ids": ("attempt-a",),
                        "environment": TrapText("SIMULATION"),
                        "account_id": "acct",
                    },
                    "environment is required",
                ),
                (
                    {
                        "attempt_ids": ("attempt-a",),
                        "environment": "SIMULATION",
                        "account_id": TrapText("acct"),
                    },
                    "account_id is required",
                ),
            ):
                with self.subTest(expected=expected):
                    with self.assertRaisesRegex(ValueError, expected):
                        unknown_submissions_from_dispatch(store, **kwargs)


if __name__ == "__main__":
    unittest.main()
