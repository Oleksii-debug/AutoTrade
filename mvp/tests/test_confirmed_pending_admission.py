from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.confirm_intent import confirm_pending_intent
from mvp.autotrade_mvp.confirmed_pending_admission import (
    admit_confirmed_pending_intent,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.pending_intent_publication import (
    publish_confirmation_required_intent,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests import test_pending_intent_publication as publication_fixtures
from mvp.tests.test_authority import (
    PUBLIC_CAPABILITY_SNAPSHOT_ID,
    public_authoritative_risk_snapshot,
    public_financial_kwargs,
    public_risk_context,
    public_risk_policy,
)


NOW = publication_fixtures.NOW


class ConfirmedPendingAdmissionTests(unittest.TestCase):
    @staticmethod
    def _fixture():
        return publication_fixtures.PendingIntentPublicationTests(
            methodName="runTest"
        )

    @staticmethod
    def _fresh_service(
        store: JournalStore,
        scope,
        *,
        risk_context=None,
    ) -> AuthorityService:
        selected_policy = public_risk_policy()

        def resolver(request):
            snapshot = public_authoritative_risk_snapshot(
                request,
                risk_context=risk_context,
                risk_policy=selected_policy,
            )
            return replace(
                snapshot,
                resolved_risk_policy=request.resolved_risk_policy,
                provider_environment=request.provider_environment,
                entity_policy_id=request.entity_policy_id,
                instrument_family=request.instrument_family,
            )

        return AuthorityService(
            store,
            risk_policy_scope=scope,
            risk_authority_resolver=resolver,
        )

    def _confirmed_source(self, store: JournalStore, *, risk_reducing=False):
        fixture = self._fixture()
        source_service, _registry, scope = fixture._service(store)
        if not risk_reducing:
            source = fixture._financial_attempt(store, source_service)
        else:
            kwargs = public_financial_kwargs(
                store,
                environment="SIMULATION",
            )
            reservations = DurableReservationBook(
                store,
                environment="SIMULATION",
                account_id="paper-1",
            )
            source = source_service.admit(
                command_id="proposal-command-admission-risk-reducing",
                idempotency_key="proposal-idem-admission-risk-reducing",
                admission_id="proposal-admission-admission-risk-reducing",
                policy_id="p1",
                intent_id="proposal-intent-admission-risk-reducing",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=publication_fixtures.INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_book=reservations,
                reservation_id="proposal-reservation-admission-risk-reducing",
                confirmation_id=None,
                risk_reducing=True,
                **kwargs,
            )
        self.assertEqual(source.reason, "confirmation_required")
        pending = publish_confirmation_required_intent(
            store,
            admission_id=source.admission_id,
            at=NOW,
        )
        confirmed = confirm_pending_intent(
            store,
            pending_intent_id=pending.pending_intent_id,
            confirmation_id="host-confirm-admit-1",
            actor_id="owner-1",
            account_id=source.account_id,
            environment=source.environment,
            accepted_at=NOW,
        )
        return source, pending, confirmed, scope

    def _runtime(self, store: JournalStore):
        values = public_financial_kwargs(
            store,
            environment="SIMULATION",
        )
        return {
            "capability_snapshot_id": PUBLIC_CAPABILITY_SNAPSHOT_ID,
            "reservation_book": DurableReservationBook(
                store,
                environment="SIMULATION",
                account_id="paper-1",
            ),
            "reservation_checkpoint_event_id": values[
                "reservation_checkpoint_event_id"
            ],
            "reservation_provider_id": values[
                "reservation_provider_id"
            ],
            "reservation_max_age_seconds": values[
                "reservation_max_age_seconds"
            ],
        }

    def test_confirmed_intent_enters_fresh_canonical_financial_admission(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            source, pending, confirmed, scope = self._confirmed_source(store)
            runtime = self._runtime(store)
            authority = self._fresh_service(store, scope)

            admitted = admit_confirmed_pending_intent(
                authority,
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=1),
                **runtime,
            )

            self.assertEqual(admitted.outcome, "ADMITTED")
            self.assertEqual(admitted.confirmation_id, confirmed.confirmation_id)
            self.assertEqual(
                admitted.requested_confirmation_id,
                confirmed.confirmation_id,
            )
            self.assertEqual(admitted.intent_hash, pending.intent_hash)
            self.assertEqual(admitted.notional, pending.notional)
            self.assertEqual(admitted.policy_version, source.policy_version)
            self.assertEqual(admitted.risk_reducing, source.risk_reducing)
            self.assertIsNotNone(admitted.reservation_id)

    def test_restart_returns_durable_consumption_instead_of_reusing_confirmation(self) -> None:
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            _source, pending, _confirmed, scope = self._confirmed_source(store)
            first = admit_confirmed_pending_intent(
                self._fresh_service(store, scope),
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=1),
                **self._runtime(store),
            )
            self.assertEqual(first.outcome, "ADMITTED")

            restarted_store = JournalStore(path)
            retry = admit_confirmed_pending_intent(
                self._fresh_service(restarted_store, scope),
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=2),
                **self._runtime(restarted_store),
            )
            self.assertEqual(retry, first)

    def test_fresh_risk_rejection_does_not_promote_confirmation_to_trade_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            _source, pending, confirmed, scope = self._confirmed_source(store)
            rejecting_context = public_risk_context()
            object.__setattr__(rejecting_context, "daily_pnl", -600)
            authority = self._fresh_service(
                store,
                scope,
                risk_context=rejecting_context,
            )

            rejected = admit_confirmed_pending_intent(
                authority,
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=1),
                **self._runtime(store),
            )
            self.assertEqual(rejected.outcome, "REJECTED")
            self.assertEqual(rejected.reason, "risk_rejected")
            self.assertIsNone(rejected.confirmation_id)
            self.assertEqual(
                rejected.requested_confirmation_id,
                confirmed.confirmation_id,
            )
            self.assertIsNone(rejected.reservation_id)

    def test_source_risk_reducing_semantics_are_preserved(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            source, pending, _confirmed, scope = self._confirmed_source(
                store,
                risk_reducing=True,
            )
            self.assertTrue(source.risk_reducing)
            self.assertFalse(pending.risk_intent.reduce_only)

            admitted = admit_confirmed_pending_intent(
                self._fresh_service(store, scope),
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=1),
                **self._runtime(store),
            )
            self.assertEqual(admitted.outcome, "ADMITTED")
            self.assertTrue(admitted.risk_reducing)

    def test_composer_exposes_no_client_financial_override_parameters(self) -> None:
        signature = inspect.signature(admit_confirmed_pending_intent)
        for forbidden in (
            "admission_id",
            "command_id",
            "idempotency_key",
            "intent_id",
            "confirmation_id",
            "actor_id",
            "risk_reducing",
            "risk_intent",
            "risk_context",
            "quantity",
            "price",
            "notional",
            "risk_policy",
            "risk_valid_until",
            "reservation_requirements",
            "reservation_available",
            "account_id",
            "environment",
            "policy_id",
            "allocation_result",
        ):
            self.assertNotIn(forbidden, signature.parameters)


if __name__ == "__main__":
    unittest.main()
