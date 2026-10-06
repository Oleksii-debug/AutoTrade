from contextlib import closing
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from types import MappingProxyType
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.durable_reservations import (
    DurableReservationBook,
    reservation_snapshot_digest,
)
from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    record_reconciliation_checkpoint,
    unknown_submissions_from_dispatch,
)
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    _event_envelope_digest,
    canonical_json,
    payload_digest,
)
from research.autotrade_research.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from mvp.autotrade_mvp.reservations import (
    InsufficientAvailable,
    ReservationConflict,
    ReservationSnapshot,
)


ARTIFACT_ID = "11111111-1111-4111-8111-111111111111"


class _HostileText(str):
    def strip(self, *_args, **_kwargs):
        raise AssertionError("hostile strip dispatched")


class _HostileMapping(dict):
    def __bool__(self):
        raise AssertionError("hostile mapping truth dispatched")

    def __len__(self):
        raise AssertionError("hostile mapping length dispatched")

    def __iter__(self):
        raise AssertionError("hostile mapping iteration dispatched")

    def items(self):
        raise AssertionError("hostile mapping items dispatched")


class DurableReservationBookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite"
        self.store = JournalStore(self.path)
        self.artifact_root = Path(self.temp.name) / "artifacts"
        self.artifacts = ArtifactStore(self.artifact_root)
        self.evidence = self.publish_resolution_evidence()

    def tearDown(self):
        self.temp.cleanup()

    def publish_resolution_evidence(
        self,
        *,
        artifact_id=ARTIFACT_ID,
        environment="PAPER",
        account_id="paper-account",
        reservation_id="r1",
        intent_id="i1",
        provider="SIMULATED",
        attempt_id="attempt-r1",
        outcome="PROVEN_ABSENT",
        reconciliation_complete=True,
        reconciliation_event=None,
    ):
        reconciliation_event_id = (
            reconciliation_event["event_id"]
            if reconciliation_event is not None
            else "00000000-0000-4000-8000-000000000000"
        )
        reconciliation_payload_hash = (
            reconciliation_event["payload_hash"]
            if reconciliation_event is not None
            else "sha256:" + "0" * 64
        )
        receipt = {
            "schema_version": 2,
            "evidence_type": "AUTOTRADE_RESERVATION_RESOLUTION",
            "environment": environment,
            "account_id": account_id,
            "reservation_id": reservation_id,
            "intent_id": intent_id,
            "provider": provider,
            "attempt_id": attempt_id,
            "outcome": outcome,
            "reconciliation_complete": reconciliation_complete,
            "reconciliation_event_id": reconciliation_event_id,
            "reconciliation_payload_hash": reconciliation_payload_hash,
        }
        manifest = self.artifacts.publish_bytes(
            artifact_id=artifact_id,
            data=canonical_json(receipt).encode("utf-8"),
            media_type="application/vnd.autotrade.reservation-resolution+json",
            rights={"storage": True, "export": False},
        )
        return f"artifact:{artifact_id}@{manifest['sha256']}"

    def book(self):
        return DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="paper-account",
            resolution_artifact_store=self.artifacts,
            resolution_artifact_root=self.artifact_root,
        )

    def create_unknown_attempt(
        self,
        *,
        attempt_id="attempt-r1",
        intent_id="i1",
        provider="SIMULATED",
    ):
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-account",
        )

        def ambiguous_transport(client_order_id, request, final_guard):
            final_guard()
            raise TimeoutError("simulated ambiguous provider result")

        def sender_check(owner_token, owner_epoch):
            self.assertTrue(owner_token)
            self.assertEqual(owner_epoch, 1)

        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash="sha256:" + "1" * 64,
            provider=provider,
            request={"instrument": "TEST", "quantity": "1"},
            now="2026-09-25T00:00:00Z",
            authority_check=lambda intent_hash, now: (True, "allowed"),
            transport_send=ambiguous_transport,
            sender_check=sender_check,
        )
        self.assertEqual(outcome.status, "UNKNOWN")
        return outcome

    def record_reconciliation_resolution(
        self,
        *,
        outcome="PROVEN_ABSENT",
        attempt_id="attempt-r1",
        reconciliation_id=None,
    ):
        unknowns = unknown_submissions_from_dispatch(
            self.store,
            attempt_ids=(attempt_id,),
            environment="PAPER",
            account_id="paper-account",
        )
        self.assertEqual(len(unknowns), 1)
        unknown = unknowns[0]
        provider_fills = ()
        local_execution_ids = ()
        searched_client_order_ids = ()
        absence_coverage = ()
        if outcome == "PROVEN_ABSENT":
            searched_client_order_ids = (unknown.client_order_id,)
            absence_coverage = tuple(
                CoverageSurfaceEvidence(
                    provider_id=unknown.provider_id,
                    account_id=unknown.account_id,
                    environment=unknown.environment,
                    surface=surface,
                    coverage_start="2026-09-24T23:59:00Z",
                    coverage_end="2026-09-25T00:05:00Z",
                    pagination_complete=True,
                    consistency_horizon_satisfied=True,
                    provider_semantics_exclude_execution=True,
                )
                for surface in (
                    "OPEN_ORDERS",
                    "ORDER_HISTORY",
                    "EXECUTIONS",
                    "ACTIVITIES",
                )
            )
        elif outcome == "FILLED":
            fill = ProviderFillEvidence.create(
                       side="BUY",
                       evidence_refs=("test:normalized-fill",),
                provider_id=unknown.provider_id,
                account_id=unknown.account_id,
                environment=unknown.environment,
                provider_execution_id="exec-" + attempt_id,
                client_order_id=unknown.client_order_id,
                instrument="TEST",
                quantity="1",
                price="1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-25T00:01:00Z",
            )
            provider_fills = (fill,)
            local_execution_ids = (fill.provider_execution_id,)
        else:
            raise ValueError("unsupported test reconciliation outcome")
        result = reconcile_account(
            provider_id=unknown.provider_id,
            account_id=unknown.account_id,
            environment=unknown.environment,
            local_cash={},
            provider_cash={},
            local_positions={},
            provider_positions={},
            local_execution_ids=local_execution_ids,
            provider_fills=provider_fills,
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id=unknown.provider_id,
                account_id=unknown.account_id,
                environment=unknown.environment,
                mode="ATOMIC",
                query_started_at="2026-09-25T00:00:00Z",
                query_completed_at="2026-09-25T00:02:00Z",
            ),
            unknown_submissions=unknowns,
            searched_client_order_ids=searched_client_order_ids,
            coverage_start="2026-09-24T23:59:00Z",
            coverage_end="2026-09-25T00:05:00Z",
            pagination_complete=True,
            absence_coverage=absence_coverage,
        )
        self.assertTrue(result.complete)
        return record_reconciliation_checkpoint(
            self.store,
            reconciliation_id=(
                reconciliation_id
                or "reconciliation-" + attempt_id + "-" + outcome.lower()
            ),
            result=result,
            observed_at="2026-09-25T00:05:00Z",
            host_id="test-host",
            owner_epoch="1",
        )

    def reserve(self, book, *, amount="70", command="cmd-reserve", idem="idem-reserve"):
        return book.reserve(
            command_id=command,
            idempotency_key=idem,
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": amount},
            available={"CASH:USD": "100"},
        )

    def test_durable_environment_text_subclass_callback_does_not_execute(self):
        book = DurableReservationBook(
            self.store,
            environment=_HostileText(" PAPER "),
            account_id=_HostileText(" paper-account "),
            resolution_artifact_store=self.artifacts,
            resolution_artifact_root=self.artifact_root,
        )
        self.assertEqual(book.environment, "PAPER")
        self.assertEqual(book.account_id, "paper-account")
        self.assertEqual(book.version, 0)

    def test_snapshot_digest_rejects_subclass_before_field_access(self):
        class HostileSnapshot(ReservationSnapshot):
            def __getattribute__(self, name):
                if name in {
                    "reservation_id",
                    "intent_id",
                    "original",
                    "remaining",
                    "consumed",
                    "state",
                    "resolution_evidence",
                }:
                    raise AssertionError("hostile snapshot field access dispatched")
                return super().__getattribute__(name)

        hostile = HostileSnapshot(
            reservation_id="r-hostile-digest",
            intent_id="i-hostile-digest",
            original=MappingProxyType({"CASH:USD": Decimal("1")}),
            remaining=MappingProxyType({"CASH:USD": Decimal("1")}),
            consumed=MappingProxyType({"CASH:USD": Decimal("0")}),
            state="WORKING",
        )
        with self.assertRaisesRegex(
            TypeError,
            "snapshot must be exact ReservationSnapshot",
        ):
            reservation_snapshot_digest(hostile)

    def test_replay_rejects_executable_event_before_mapping_callbacks(self):
        book = self.book()
        hostile_event = _HostileMapping()
        dict.__setitem__(hostile_event, "aggregate_version", 1)

        with self.assertRaisesRegex(
            ReservationConflict,
            "exact inert JSON values",
        ):
            book._replay([hostile_event])

    def test_replay_rejects_executable_nested_json_before_digest_callbacks(self):
        book = self.book()

        for nested_name in ("payload", "request", "snapshot"):
            hostile = _HostileMapping()
            dict.__setitem__(hostile, "hostile", "value")
            if nested_name == "payload":
                event = {
                    "aggregate_version": 1,
                    "event_type": "ignored",
                    "payload": hostile,
                    "payload_hash": "ignored",
                }
            else:
                payload = {
                    nested_name: hostile,
                    "safe": "value",
                }
                event = {
                    "aggregate_version": 1,
                    "event_type": "ignored",
                    "payload": payload,
                    "payload_hash": "ignored",
                }

            with self.subTest(nested_name=nested_name):
                with self.assertRaisesRegex(
                    ReservationConflict,
                    "exact inert JSON values",
                ):
                    book._replay([event])

    def test_durable_text_subclass_callbacks_do_not_execute(self):
        book = self.book()
        snapshot = book.reserve(
            command_id=_HostileText(" cmd-hostile-text "),
            idempotency_key=_HostileText(" idem-hostile-text "),
            reservation_id=_HostileText(" r-hostile-text "),
            intent_id=_HostileText(" i-hostile-text "),
            requirements={_HostileText(" CASH:USD "): "10"},
            available={"CASH:USD": "100"},
        )

        self.assertEqual(snapshot.reservation_id, "r-hostile-text")
        self.assertEqual(snapshot.intent_id, "i-hostile-text")
        self.assertEqual(tuple(snapshot.original), ("CASH:USD",))
        self.assertEqual(book.version, 1)
        restarted = self.book()
        self.assertEqual(restarted.get("r-hostile-text"), snapshot)

    def test_durable_reserve_rejects_hostile_mapping_before_journal_mutation(self):
        for mapping_kind in ("requirements", "available"):
            hostile = _HostileMapping()
            dict.__setitem__(
                hostile,
                "CASH:USD",
                "10" if mapping_kind == "requirements" else "100",
            )
            for candidate in (hostile, MappingProxyType(hostile)):
                with self.subTest(
                    mapping_kind=mapping_kind,
                    mapping_type=type(candidate).__name__,
                ):
                    book = self.book()
                    before = book.version
                    kwargs = {
                        "requirements": {"CASH:USD": "10"},
                        "available": {"CASH:USD": "100"},
                    }
                    kwargs[mapping_kind] = candidate
                    with self.assertRaisesRegex(
                        TypeError,
                        "resource amounts must use an exact dict",
                    ):
                        book.reserve(
                            command_id=f"cmd-hostile-{mapping_kind}",
                            idempotency_key=f"idem-hostile-{mapping_kind}",
                            reservation_id=f"r-hostile-{mapping_kind}",
                            intent_id=f"i-hostile-{mapping_kind}",
                            **kwargs,
                        )
                    self.assertEqual(book.version, before)
                    self.assertEqual(book.active(), ())
                    self.assertEqual(self.book().version, before)

    def test_durable_consume_rejects_hostile_mapping_before_journal_mutation(self):
        for candidate_factory in (
            lambda hostile: hostile,
            MappingProxyType,
        ):
            with self.subTest(factory=getattr(candidate_factory, "__name__", "proxy")):
                book = self.book()
                reservation_id = "r-hostile-consume-" + (
                    "proxy" if candidate_factory is MappingProxyType else "dict"
                )
                intent_id = "i-hostile-consume-" + (
                    "proxy" if candidate_factory is MappingProxyType else "dict"
                )
                book.reserve(
                    command_id="cmd-" + reservation_id,
                    idempotency_key="idem-" + reservation_id,
                    reservation_id=reservation_id,
                    intent_id=intent_id,
                    requirements={"CASH:USD": "10"},
                    available={"CASH:USD": "100"},
                )
                before = book.get(reservation_id)
                before_version = book.version
                hostile = _HostileMapping()
                dict.__setitem__(hostile, "CASH:USD", "1")
                candidate = candidate_factory(hostile)
                with self.assertRaisesRegex(
                    TypeError,
                    "resource amounts must use an exact dict",
                ):
                    book.consume(
                        command_id="cmd-consume-" + reservation_id,
                        idempotency_key="idem-consume-" + reservation_id,
                        reservation_id=reservation_id,
                        usage=candidate,
                    )
                self.assertEqual(book.version, before_version)
                self.assertEqual(book.get(reservation_id), before)
                restarted = self.book()
                self.assertEqual(restarted.version, before_version)
                self.assertEqual(restarted.get(reservation_id), before)

    def test_exact_consumption_replays_identically_across_decimal_contexts(self):
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_FLOOR
            first = self.book()
            first.reserve(
                command_id="cmd-exact-reserve",
                idempotency_key="idem-exact-reserve",
                reservation_id="r-exact",
                intent_id="i-exact",
                requirements={"CASH:USD": "100000.0000001"},
                available={"CASH:USD": "200000"},
            )
            first.consume(
                command_id="cmd-exact-consume",
                idempotency_key="idem-exact-consume",
                reservation_id="r-exact",
                usage={"CASH:USD": "0.0000001"},
            )
            before = first.get("r-exact")
            before_total = first.total_reserved("CASH:USD")

        with localcontext() as context:
            context.prec = 80
            context.rounding = ROUND_HALF_EVEN
            restarted = self.book()
            after = restarted.get("r-exact")
            after_total = restarted.total_reserved("CASH:USD")

        self.assertEqual(before, after)
        self.assertEqual(before.remaining["CASH:USD"], Decimal("100000"))
        self.assertEqual(before.consumed["CASH:USD"], Decimal("0.0000001"))
        self.assertEqual(before_total, Decimal("100000"))
        self.assertEqual(after_total, Decimal("100000"))
        self.assertEqual(restarted.version, 2)

    def test_durable_reserve_rejects_oversized_amount_before_journal_mutation(self):
        book = self.book()
        before = book.version
        with self.assertRaisesRegex(ValueError, "resource envelope"):
            book.reserve(
                command_id="cmd-oversized",
                idempotency_key="idem-oversized",
                reservation_id="r-oversized",
                intent_id="i-oversized",
                requirements={"CASH:USD": "1e1000"},
                available={"CASH:USD": "1e1000"},
            )
        self.assertEqual(book.version, before)
        self.assertEqual(book.active(), ())
        self.assertEqual(self.book().version, before)

    def test_returned_snapshot_mutation_cannot_change_durable_capacity_or_restart(self):
        book = self.book()
        returned = self.reserve(book)

        object.__setattr__(returned, "state", "CANCELED")
        object.__setattr__(returned, "remaining", {"CASH:USD": Decimal("0")})

        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(book.get("r1").state, "WORKING")
        with self.assertRaises(InsufficientAvailable):
            book.reserve(
                command_id="cmd-overallocate-after-mutation",
                idempotency_key="idem-overallocate-after-mutation",
                reservation_id="r2",
                intent_id="i2",
                requirements={"CASH:USD": "40"},
                available={"CASH:USD": "100"},
            )

        restarted = self.book()
        self.assertEqual(restarted.get("r1").state, "WORKING")
        self.assertEqual(
            restarted.get("r1").remaining["CASH:USD"],
            Decimal("70"),
        )
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))
        with self.assertRaises(InsufficientAvailable):
            restarted.reserve(
                command_id="cmd-overallocate-after-restart",
                idempotency_key="idem-overallocate-after-restart",
                reservation_id="r3",
                intent_id="i3",
                requirements={"CASH:USD": "40"},
                available={"CASH:USD": "100"},
            )

    def test_restart_reconstructs_active_reservation_from_journal(self):
        first = self.book()
        self.reserve(first)
        self.assertEqual(first.version, 1)
        self.assertEqual(first.total_reserved("CASH:USD"), Decimal("70"))

        restarted = self.book()
        self.assertEqual(restarted.version, 1)
        self.assertEqual(restarted.get("r1").state, "WORKING")
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))

    def test_partial_consumption_and_unknown_survive_restart(self):
        first = self.book()
        self.reserve(first)
        first.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "20"},
        )
        first.mark_unknown(
            command_id="cmd-unknown",
            idempotency_key="idem-unknown",
            reservation_id="r1",
        )

        restarted = self.book()
        snapshot = restarted.get("r1")
        self.assertEqual(snapshot.state, "UNKNOWN")
        self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("20"))
        self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("50"))
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("50"))
        self.assertEqual(restarted.version, 3)

    def test_evidenced_terminal_resolution_survives_restart(self):
        first = self.book()
        self.reserve(first)
        first.mark_unknown(
            command_id="cmd-unknown",
            idempotency_key="idem-unknown",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        reconciliation = self.record_reconciliation_resolution()
        evidence = self.publish_resolution_evidence(
            artifact_id="66666666-6666-4666-8666-666666666666",
            reconciliation_event=reconciliation,
        )
        first.mark_terminal(
            command_id="cmd-terminal",
            idempotency_key="idem-terminal",
            reservation_id="r1",
            outcome="PROVEN_ABSENT",
            provider="SIMULATED",
            attempt_id="attempt-r1",
            resolution_evidence=evidence,
        )

        restarted = self.book()
        snapshot = restarted.get("r1")
        self.assertEqual(snapshot.state, "PROVEN_ABSENT")
        self.assertEqual(
            snapshot.resolution_evidence,
            evidence,
        )
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("0"))

    def test_terminal_release_rejects_cross_scope_reconciliation_after_restart(self):
        book = self.book()
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-cross-scope",
            idempotency_key="idem-unknown-cross-scope",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        reconciliation = self.record_reconciliation_resolution()

        cases = (
            (
                "provider_id",
                "OTHER-PROVIDER",
                "88888888-8888-4888-8888-888888888881",
                "00000000-0000-4000-8000-000000000081",
            ),
            (
                "account_id",
                "other-account",
                "88888888-8888-4888-8888-888888888882",
                "00000000-0000-4000-8000-000000000082",
            ),
            (
                "environment",
                "LIVE",
                "88888888-8888-4888-8888-888888888883",
                "00000000-0000-4000-8000-000000000083",
            ),
        )
        restart_evidence = None
        for field, wrong_value, artifact_id, event_id in cases:
            with self.subTest(field=field):
                cross_payload = dict(reconciliation["payload"])
                cross_payload[field] = wrong_value
                cross_event = dict(reconciliation)
                cross_event["event_id"] = event_id
                cross_event["aggregate_id"] = "cross-scope-" + field
                cross_event["aggregate_version"] = "1"
                cross_event["payload"] = cross_payload
                cross_event["payload_hash"] = payload_digest(cross_payload)
                self.store.append_event(cross_event)
                evidence = self.publish_resolution_evidence(
                    artifact_id=artifact_id,
                    reconciliation_event=cross_event,
                )
                if field == "account_id":
                    restart_evidence = evidence
                with self.assertRaisesRegex(
                    ReservationConflict,
                    "reconciliation checkpoint scope",
                ):
                    book.mark_terminal(
                        command_id="cmd-terminal-cross-" + field,
                        idempotency_key="idem-terminal-cross-" + field,
                        reservation_id="r1",
                        outcome="PROVEN_ABSENT",
                        provider="SIMULATED",
                        attempt_id="attempt-r1",
                        resolution_evidence=evidence,
                    )

        restarted = self.book()
        self.assertEqual(restarted.get("r1").state, "UNKNOWN")
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))
        self.assertIsNotNone(restart_evidence)
        with self.assertRaisesRegex(
            ReservationConflict,
            "reconciliation checkpoint scope",
        ):
            restarted.mark_terminal(
                command_id="cmd-terminal-cross-restart",
                idempotency_key="idem-terminal-cross-restart",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=restart_evidence,
            )
        self.assertEqual(restarted.get("r1").state, "UNKNOWN")
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))

    def test_observed_execution_does_not_prove_full_fill_or_release_buffer(self):
        book = self.book()
        self.reserve(book, amount="70")
        book.consume(
            command_id="cmd-consume-filled",
            idempotency_key="idem-consume-filled",
            reservation_id="r1",
            usage={"CASH:USD": "60"},
        )
        book.mark_unknown(
            command_id="cmd-unknown-filled",
            idempotency_key="idem-unknown-filled",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        reconciliation = self.record_reconciliation_resolution(outcome="FILLED")
        filled_evidence = self.publish_resolution_evidence(
            artifact_id="55555555-5555-4555-8555-555555555555",
            outcome="FILLED",
            reconciliation_event=reconciliation,
        )
        before = book.total_reserved("CASH:USD")
        with self.assertRaisesRegex(
            ReservationConflict,
            "lacks canonical reconciliation semantics",
        ):
            book.mark_terminal(
                command_id="cmd-terminal-filled",
                idempotency_key="idem-terminal-filled",
                reservation_id="r1",
                outcome="FILLED",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=filled_evidence,
            )
        self.assertEqual(book.get("r1").state, "UNKNOWN")
        self.assertEqual(book.get("r1").consumed["CASH:USD"], Decimal("60"))
        self.assertEqual(book.total_reserved("CASH:USD"), before)

        restarted = self.book()
        restored = restarted.get("r1")
        self.assertEqual(restored.state, "UNKNOWN")
        self.assertEqual(restored.consumed["CASH:USD"], Decimal("60"))
        self.assertEqual(restarted.total_reserved("CASH:USD"), before)

    def test_restart_does_not_make_reserved_cash_available_again(self):
        first = self.book()
        self.reserve(first, amount="70")

        restarted = self.book()
        with self.assertRaises(InsufficientAvailable):
            restarted.reserve(
                command_id="cmd-r2",
                idempotency_key="idem-r2",
                reservation_id="r2",
                intent_id="i2",
                requirements={"CASH:USD": "40"},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(restarted.version, 1)

    def test_duplicate_consume_idempotency_key_does_not_consume_twice(self):
        book = self.book()
        self.reserve(book, amount="100")
        first = book.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "40"},
        )
        second = book.consume(
            command_id="another-command-id-is-ignored-by-idem",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "40"},
        )
        self.assertEqual(first, second)
        self.assertEqual(second.consumed["CASH:USD"], Decimal("40"))
        self.assertEqual(second.remaining["CASH:USD"], Decimal("60"))
        self.assertEqual(book.version, 2)

    def test_idempotency_key_cannot_be_reused_for_different_economics(self):
        book = self.book()
        self.reserve(book, amount="100")
        book.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "10"},
        )
        with self.assertRaisesRegex(ReservationConflict, "different"):
            book.consume(
                command_id="cmd-consume-other",
                idempotency_key="idem-consume",
                reservation_id="r1",
                usage={"CASH:USD": "11"},
            )
        self.assertEqual(book.get("r1").consumed["CASH:USD"], Decimal("10"))

    def test_command_and_idempotency_namespaces_are_isolated_per_account(self):
        first = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="account-a",
        )
        second = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="account-b",
        )

        first.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="r-a",
            intent_id="i-a",
            requirements={"CASH:USD": "70"},
            available={"CASH:USD": "100"},
        )
        second.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="r-b",
            intent_id="i-b",
            requirements={"CASH:USD": "60"},
            available={"CASH:USD": "100"},
        )

        self.assertEqual(first.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(second.total_reserved("CASH:USD"), Decimal("60"))
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="PAPER",
                    account_id="account-a",
            ).total_reserved("CASH:USD"),
            Decimal("70"),
        )
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="PAPER",
                    account_id="account-b",
            ).total_reserved("CASH:USD"),
            Decimal("60"),
        )

    def test_same_account_isolated_between_paper_and_live(self):
        paper = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="same-account",
        )
        live = DurableReservationBook(
            self.store,
            environment="LIVE",
            account_id="same-account",
        )
        paper.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="paper-r",
            intent_id="paper-i",
            requirements={"CASH:USD": "70"},
            available={"CASH:USD": "100"},
        )
        live.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="live-r",
            intent_id="live-i",
            requirements={"CASH:USD": "30"},
            available={"CASH:USD": "100"},
        )
        self.assertNotEqual(paper.scope_id, live.scope_id)
        self.assertEqual(paper.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(live.total_reserved("CASH:USD"), Decimal("30"))
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="PAPER",
                account_id="same-account",
            ).total_reserved("CASH:USD"),
            Decimal("70"),
        )
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="LIVE",
                account_id="same-account",
            ).total_reserved("CASH:USD"),
            Decimal("30"),
        )

    def test_reservation_environment_is_required_and_canonical(self):
        with self.assertRaises(TypeError):
            DurableReservationBook(self.store, account_id="acct")
        with self.assertRaisesRegex(ValueError, "environment must be"):
            DurableReservationBook(
                self.store,
                environment="",
                account_id="acct",
            )
        lower = DurableReservationBook(
            self.store,
            environment=" paper ",
            account_id="acct",
        )
        upper = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="acct",
        )
        self.assertEqual(lower.environment, "PAPER")
        self.assertEqual(lower.scope_id, upper.scope_id)

    def test_journal_failure_does_not_mutate_projection(self):
        book = self.book()
        with patch.object(
            JournalStore,
            "commit_command",
            autospec=True,
            side_effect=RuntimeError("simulated disk failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                self.reserve(book)
        self.assertEqual(book.version, 0)
        self.assertEqual(book.active(), ())
        self.assertEqual(self.book().active(), ())

    def test_tampered_payload_is_rejected_on_restart(self):
        book = self.book()
        self.reserve(book)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT event_id, payload_json FROM events "
                "WHERE aggregate_type='reservation_book'"
            ).fetchone()
            connection.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?",
                (row[1].replace('"70"', '"71"', 1), row[0]),
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "payload hash"):
            self.book()

    def test_tampered_snapshot_with_recomputed_hash_still_fails_replay(self):
        book = self.book()
        self.reserve(book)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT event_id, payload_json, envelope_json FROM events "
                "WHERE aggregate_type='reservation_book'"
            ).fetchone()
            import json

            payload = json.loads(row[1])
            payload["snapshot"]["remaining"]["CASH:USD"] = "69"
            replacement = canonical_json(payload)
            envelope = json.loads(row[2])
            envelope["payload"] = payload
            envelope["payload_hash"] = payload_digest(payload)
            envelope_json = canonical_json(envelope)
            connection.execute(
                "UPDATE events SET payload_json=?, payload_hash=?, "
                "envelope_json=?, envelope_hash=? WHERE event_id=?",
                (
                    replacement,
                    payload_digest(payload),
                    envelope_json,
                    _event_envelope_digest(envelope_json),
                    row[0],
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(ReservationConflict, "snapshot"):
            self.book()

    def test_replay_rejects_rehashed_cross_environment_event_scope(self):
        book = self.book()
        self.reserve(book)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT event_id, payload_json, envelope_json FROM events "
                "WHERE aggregate_type='reservation_book'"
            ).fetchone()
            import json

            payload = json.loads(row[1])
            payload["environment"] = "LIVE"
            replacement = canonical_json(payload)
            envelope = json.loads(row[2])
            envelope["payload"] = payload
            envelope["payload_hash"] = payload_digest(payload)
            envelope_json = canonical_json(envelope)
            connection.execute(
                "UPDATE events SET payload_json=?, payload_hash=?, "
                "envelope_json=?, envelope_hash=? WHERE event_id=?",
                (
                    replacement,
                    payload_digest(payload),
                    envelope_json,
                    _event_envelope_digest(envelope_json),
                    row[0],
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(ReservationConflict, "environment.*scope"):
            self.book()

    def test_replay_rejects_rehashed_cross_account_event_scope(self):
        book = self.book()
        self.reserve(book)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT event_id, payload_json, envelope_json FROM events "
                "WHERE aggregate_type='reservation_book'"
            ).fetchone()
            import json

            payload = json.loads(row[1])
            payload["account_id"] = "other-account"
            replacement = canonical_json(payload)
            envelope = json.loads(row[2])
            envelope["payload"] = payload
            envelope["payload_hash"] = payload_digest(payload)
            envelope_json = canonical_json(envelope)
            connection.execute(
                "UPDATE events SET payload_json=?, payload_hash=?, "
                "envelope_json=?, envelope_hash=? WHERE event_id=?",
                (
                    replacement,
                    payload_digest(payload),
                    envelope_json,
                    _event_envelope_digest(envelope_json),
                    row[0],
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(ReservationConflict, "account.*scope"):
            self.book()

    def test_unknown_still_cannot_erase_consumed_exposure(self):
        book = self.book()
        self.reserve(book, amount="100")
        book.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "10"},
        )
        book.mark_unknown(
            command_id="cmd-unknown",
            idempotency_key="idem-unknown",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        reconciliation = self.record_reconciliation_resolution()
        evidence = self.publish_resolution_evidence(
            artifact_id="77777777-7777-4777-8777-777777777777",
            reconciliation_event=reconciliation,
        )
        with self.assertRaises(ReservationConflict):
            book.mark_terminal(
                command_id="cmd-absent",
                idempotency_key="idem-absent",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=evidence,
            )
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("90"))

    def test_concurrent_same_idempotency_between_reads_is_not_double_applied(self):
        book = self.book()
        self.reserve(book, amount="100")
        original_events_class = DurableReservationBook._events
        original_events = book._events
        calls = 0
        raced = False

        def race_on_second_read(selected):
            nonlocal calls, raced
            if selected is not book:
                return original_events_class(selected)
            calls += 1
            if calls == 2 and not raced:
                raced = True
                competing = DurableReservationBook(
                    JournalStore(self.path),
                    environment="PAPER",
                    account_id="paper-account",
                )
                competing.consume(
                    command_id="cmd-competing-consume",
                    idempotency_key="idem-shared-consume",
                    reservation_id="r1",
                    usage={"CASH:USD": "60"},
                )
            return original_events()

        with patch.object(DurableReservationBook, "_events", autospec=True, side_effect=race_on_second_read):
            result = book.consume(
                command_id="cmd-local-consume",
                idempotency_key="idem-shared-consume",
                reservation_id="r1",
                usage={"CASH:USD": "60"},
            )

        self.assertEqual(result.consumed["CASH:USD"], Decimal("60"))
        self.assertEqual(result.remaining["CASH:USD"], Decimal("40"))
        self.assertEqual(book.version, 2)
        restarted = self.book()
        self.assertEqual(restarted.get("r1").consumed["CASH:USD"], Decimal("60"))
        self.assertEqual(restarted.version, 2)

    def test_concurrent_writer_fences_stale_projection_without_corrupting_journal(self):
        book = self.book()
        original_commit_class = JournalStore.commit_command
        original_commit = self.store.commit_command
        competing = DurableReservationBook(
            JournalStore(self.path), environment="PAPER", account_id="paper-account",
        )
        raced = False

        def race_then_commit(selected, *args, **kwargs):
            nonlocal raced
            if selected is not self.store:
                return original_commit_class(selected, *args, **kwargs)
            if not raced:
                raced = True
                competing.reserve(
                    command_id="cmd-race",
                    idempotency_key="idem-race",
                    reservation_id="r-race",
                    intent_id="i-race",
                    requirements={"CASH:USD": "30"},
                    available={"CASH:USD": "100"},
                )
            return original_commit(*args, **kwargs)

        with patch.object(
            JournalStore,
            "commit_command",
            autospec=True,
            side_effect=race_then_commit,
        ):
            with self.assertRaisesRegex(ValueError, "aggregate_version must be 2"):
                book.reserve(
                    command_id="cmd-stale",
                    idempotency_key="idem-stale",
                    reservation_id="r-stale",
                    intent_id="i-stale",
                    requirements={"CASH:USD": "80"},
                    available={"CASH:USD": "100"},
                )

        self.assertEqual(
            [item.reservation_id for item in book.active()],
            ["r-race"],
        )
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("30"))
        self.assertEqual(book.version, 1)
        restarted = self.book()
        self.assertEqual(restarted.version, 1)
        self.assertEqual(
            [item.reservation_id for item in restarted.active()],
            ["r-race"],
        )
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("30"))

    def test_resource_alias_collision_fails_before_journal_mutation(self):
        book = self.book()
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            book.reserve(
                command_id="cmd-alias",
                idempotency_key="idem-alias",
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": "10", " CASH:USD ": "20"},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(book.version, 0)

    def test_terminal_release_requires_trusted_artifact_store(self):
        book = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="paper-account",
        )
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-no-store",
            idempotency_key="idem-unknown-no-store",
            reservation_id="r1",
        )
        before = book.total_reserved("CASH:USD")
        with self.assertRaisesRegex(
            ReservationConflict,
            "trusted resolution artifact store",
        ):
            book.mark_terminal(
                command_id="cmd-terminal-no-store",
                idempotency_key="idem-terminal-no-store",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=self.evidence,
            )
        self.assertEqual(book.get("r1").state, "UNKNOWN")
        self.assertEqual(book.total_reserved("CASH:USD"), before)
        self.assertEqual(book.version, 2)

    def test_always_true_callback_cannot_be_installed_as_resolution_authority(self):
        with self.assertRaises(TypeError):
            DurableReservationBook(
                self.store,
                environment="PAPER",
                account_id="paper-account",
                resolution_artifact_store=lambda reference: True,
                resolution_artifact_root=self.artifact_root,
            )

    def test_terminal_release_rejects_artifact_store_subclass_authority(self):
        class ForgedArtifactStore(ArtifactStore):
            def read_authenticated_snapshot(self, artifact_id):
                raise AssertionError("subclass evidence authority must not be called")

        forged = ForgedArtifactStore(Path(self.temp.name) / "forged-artifacts")
        with self.assertRaisesRegex(TypeError, "canonical ArtifactStore"):
            DurableReservationBook(
                self.store,
                environment="PAPER",
                account_id="paper-account",
                resolution_artifact_store=forged,
                resolution_artifact_root=self.artifact_root,
            )

    def test_terminal_release_rejects_stable_attacker_selected_store_root(self):
        attacker_root = Path(self.temp.name) / "attacker-selected-artifacts"
        attacker_store = ArtifactStore(attacker_root)
        attacker_store.publish_bytes(
            artifact_id=ARTIFACT_ID,
            data=b'{"forged":true}',
            media_type="application/vnd.autotrade.reservation-resolution+json",
            rights={"storage": True, "export": False},
        )

        with self.assertRaisesRegex(
            ArtifactIntegrityError,
            "does not match trusted artifact root",
        ):
            DurableReservationBook(
                self.store,
                environment="PAPER",
                account_id="paper-account",
                resolution_artifact_store=attacker_store,
                resolution_artifact_root=self.artifact_root,
            )

    def test_terminal_release_private_reader_ignores_post_construction_store_poisoning(self):
        book = self.book()
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-private-reader",
            idempotency_key="idem-unknown-private-reader",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        reconciliation = self.record_reconciliation_resolution()
        evidence = self.publish_resolution_evidence(
            artifact_id="99999999-9999-4999-8999-999999999999",
            reconciliation_event=reconciliation,
        )

        redirected = Path(self.temp.name) / "redirected-artifacts"
        object.__setattr__(self.artifacts, "root", redirected)
        object.__setattr__(
            self.artifacts,
            "_read_verified_object_bytes",
            lambda _manifest: b"forged resolution",
        )
        object.__setattr__(
            self.artifacts,
            "_manifest_path",
            lambda _artifact_id: redirected / "forged.json",
        )

        book.mark_terminal(
            command_id="cmd-terminal-private-reader",
            idempotency_key="idem-terminal-private-reader",
            reservation_id="r1",
            outcome="PROVEN_ABSENT",
            provider="SIMULATED",
            attempt_id="attempt-r1",
            resolution_evidence=evidence,
        )
        self.assertEqual(book.get("r1").state, "PROVEN_ABSENT")

    def test_terminal_release_private_reader_ignores_pre_construction_store_poisoning(self):
        writer = self.book()
        self.reserve(writer)
        writer.mark_unknown(
            command_id="cmd-unknown-prebound-reader",
            idempotency_key="idem-unknown-prebound-reader",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        reconciliation = self.record_reconciliation_resolution()
        evidence = self.publish_resolution_evidence(
            artifact_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            reconciliation_event=reconciliation,
        )

        redirected = Path(self.temp.name) / "redirected-before-construction"
        object.__setattr__(self.artifacts, "root", redirected)
        object.__setattr__(
            self.artifacts,
            "_decode_manifest_bytes",
            lambda *_args, **_kwargs: {"forged": True},
        )
        object.__setattr__(
            self.artifacts,
            "_read_verified_object_bytes",
            lambda _manifest: b"forged resolution",
        )

        restarted = DurableReservationBook(
            JournalStore(self.path),
            environment="PAPER",
            account_id="paper-account",
            resolution_artifact_store=self.artifacts,
            resolution_artifact_root=self.artifact_root,
        )
        restarted.mark_terminal(
            command_id="cmd-terminal-prebound-reader",
            idempotency_key="idem-terminal-prebound-reader",
            reservation_id="r1",
            outcome="PROVEN_ABSENT",
            provider="SIMULATED",
            attempt_id="attempt-r1",
            resolution_evidence=evidence,
        )
        self.assertEqual(restarted.get("r1").state, "PROVEN_ABSENT")

    def test_terminal_release_requires_existing_durable_attempt(self):
        book = self.book()
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-no-attempt",
            idempotency_key="idem-unknown-no-attempt",
            reservation_id="r1",
        )
        with self.assertRaisesRegex(
            ReservationConflict,
            "not bound to a durable submission attempt",
        ):
            book.mark_terminal(
                command_id="cmd-terminal-no-attempt",
                idempotency_key="idem-terminal-no-attempt",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=self.evidence,
            )
        self.assertEqual(book.get("r1").state, "UNKNOWN")
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("70"))

    def test_terminal_release_rejects_bool_integer_receipt_alias(self):
        aliased = self.publish_resolution_evidence(
            artifact_id="33333333-3333-4333-8333-333333333333",
            reconciliation_complete=1,
        )
        book = self.book()
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-bool-alias",
            idempotency_key="idem-unknown-bool-alias",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        with self.assertRaisesRegex(
            ReservationConflict,
            "does not match reservation scope",
        ):
            book.mark_terminal(
                command_id="cmd-terminal-bool-alias",
                idempotency_key="idem-terminal-bool-alias",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=aliased,
            )
        self.assertEqual(book.get("r1").state, "UNKNOWN")

    def test_terminal_release_fails_closed_on_wrong_receipt_scope(self):
        wrong_evidence = self.publish_resolution_evidence(
            artifact_id="22222222-2222-4222-8222-222222222222",
            account_id="other-account",
        )
        book = self.book()
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-wrong-scope",
            idempotency_key="idem-unknown-wrong-scope",
            reservation_id="r1",
        )
        with self.assertRaisesRegex(
            ReservationConflict,
            "does not match reservation scope",
        ):
            book.mark_terminal(
                command_id="cmd-terminal-rejected-evidence",
                idempotency_key="idem-terminal-rejected-evidence",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=wrong_evidence,
            )
        self.assertEqual(book.get("r1").state, "UNKNOWN")
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(book.version, 2)

    def test_restart_reverifies_committed_resolution_artifact(self):
        book = self.book()
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-reverify",
            idempotency_key="idem-unknown-reverify",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        reconciliation = self.record_reconciliation_resolution()
        evidence = self.publish_resolution_evidence(
            artifact_id="88888888-8888-4888-8888-888888888888",
            reconciliation_event=reconciliation,
        )
        book.mark_terminal(
            command_id="cmd-terminal-reverify",
            idempotency_key="idem-terminal-reverify",
            reservation_id="r1",
            outcome="PROVEN_ABSENT",
            provider="SIMULATED",
            attempt_id="attempt-r1",
            resolution_evidence=evidence,
        )
        manifest = self.artifacts.load_manifest(
            "88888888-8888-4888-8888-888888888888"
        )
        digest = manifest["sha256"].removeprefix("sha256:")
        object_path = self.artifacts.objects / digest[:2] / digest
        object_path.write_bytes(b"corrupt")

        with self.assertRaisesRegex(
            ReservationConflict,
            "cannot be replayed",
        ):
            self.book()

    def test_self_authored_resolution_receipt_cannot_release_without_checkpoint(self):
        book = self.book()
        self.reserve(book)
        book.mark_unknown(
            command_id="cmd-unknown-self-authored",
            idempotency_key="idem-unknown-self-authored",
            reservation_id="r1",
        )
        self.create_unknown_attempt()
        before = book.total_reserved("CASH:USD")
        with self.assertRaisesRegex(
            ReservationConflict,
            "matching durable reconciliation checkpoint",
        ):
            book.mark_terminal(
                command_id="cmd-terminal-self-authored",
                idempotency_key="idem-terminal-self-authored",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=self.evidence,
            )
        self.assertEqual(book.get("r1").state, "UNKNOWN")
        self.assertEqual(book.total_reserved("CASH:USD"), before)
        self.assertEqual(self.book().get("r1").state, "UNKNOWN")

    def test_terminal_release_requires_immutable_evidence_before_journal_mutation(self):
        book = self.book()
        self.reserve(book)
        before_version = book.version
        before_reserved = book.total_reserved("CASH:USD")
        with self.assertRaisesRegex(ValueError, "immutable artifact"):
            book.mark_terminal(
                command_id="cmd-terminal-weak",
                idempotency_key="idem-terminal-weak",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence="provider-complete-coverage",
            )
        self.assertEqual(book.version, before_version)
        self.assertEqual(book.total_reserved("CASH:USD"), before_reserved)
        self.assertEqual(self.book().total_reserved("CASH:USD"), before_reserved)

    def test_terminal_release_rejects_noncanonical_evidence_digest(self):
        book = self.book()
        self.reserve(book)
        with self.assertRaisesRegex(ValueError, "canonical lowercase"):
            book.mark_terminal(
                command_id="cmd-terminal-bad-digest",
                idempotency_key="idem-terminal-bad-digest",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                provider="SIMULATED",
                attempt_id="attempt-r1",
                resolution_evidence=(
                    "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                    + "A" * 64
                ),
            )
        self.assertEqual(book.version, 1)

    def test_binary_float_inputs_fail_before_journal_mutation(self):
        book = self.book()
        with self.assertRaises(TypeError):
            book.reserve(
                command_id="cmd-float",
                idempotency_key="idem-float",
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": 10.5},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(book.version, 0)


    def test_long_lived_reader_refreshes_after_external_reservation_mutation(self):
        reader = self.book()
        writer = DurableReservationBook(
            JournalStore(self.path),
            environment="PAPER",
            account_id="paper-account",
        )
        self.assertEqual(reader.total_reserved("CASH:USD"), Decimal("0"))
        self.assertEqual(reader.active(), ())

        writer.reserve(
            command_id="cmd-external",
            idempotency_key="idem-external",
            reservation_id="r-external",
            intent_id="i-external",
            requirements={"CASH:USD": "70"},
            available={"CASH:USD": "100"},
        )

        self.assertEqual(reader.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(
            [item.reservation_id for item in reader.active()],
            ["r-external"],
        )
        self.assertEqual(reader.get("r-external").state, "WORKING")

    def test_commit_command_receives_canonical_actor_and_environment(self):
        book = self.book()
        original = self.store.commit_command
        observed = {}

        def capture(_store, **kwargs):
            observed.update(kwargs)
            return original(**kwargs)

        with patch.object(JournalStore, "commit_command", autospec=True, side_effect=capture):
            self.reserve(book)

        self.assertEqual(observed["actor"], "autotrade-reservation-authority")
        self.assertEqual(observed["environment"], "PAPER")
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("70"))

    def test_scoped_command_contract_survives_restart_after_reserve(self):
        first = self.book()
        self.reserve(first)
        restarted = DurableReservationBook(
            JournalStore(self.path),
            environment="PAPER",
            account_id="paper-account",
            resolution_artifact_store=self.artifacts,
            resolution_artifact_root=self.artifact_root,
        )
        self.assertEqual(restarted.version, 1)
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(restarted.get("r1").state, "WORKING")


if __name__ == "__main__":
    unittest.main()
