from __future__ import annotations

from datetime import timedelta
import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.confirm_intent import confirm_pending_intent
from mvp.autotrade_mvp.confirmed_pending_source import (
    resolve_confirmed_pending_source_admission,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.pending_intent_publication import (
    publish_confirmation_required_intent,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests import test_pending_intent_publication as publication_fixtures
from mvp.tests.test_authority import (
    INSTRUMENT_ID,
    public_financial_kwargs,
)


NOW = publication_fixtures.NOW


class ConfirmedPendingSourceTests(unittest.TestCase):
    @staticmethod
    def _fixture():
        return publication_fixtures.PendingIntentPublicationTests(
            methodName="runTest"
        )

    def _confirm(self, store: JournalStore, record):
        pending = publish_confirmation_required_intent(
            store,
            admission_id=record.admission_id,
            at=NOW,
        )
        confirmed = confirm_pending_intent(
            store,
            pending_intent_id=pending.pending_intent_id,
            confirmation_id="host-confirm-source-1",
            actor_id="owner-1",
            account_id=record.account_id,
            environment=record.environment,
            accepted_at=NOW,
        )
        return pending, confirmed

    def test_restart_recovers_exact_source_confirmation_required_admission(self) -> None:
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            fixture = self._fixture()
            service, _registry, _scope = fixture._service(store)
            source = fixture._financial_attempt(store, service)
            self.assertEqual(source.reason, "confirmation_required")
            pending, _confirmed = self._confirm(store, source)

            resolved = resolve_confirmed_pending_source_admission(
                JournalStore(path),
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(resolved, source)
            self.assertEqual(resolved.risk_decision_id, source.risk_decision_id)
            self.assertEqual(
                resolved.financial_command_id,
                source.financial_command_id,
            )

    def test_pending_id_selects_one_source_among_similar_rejections(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            fixture = self._fixture()
            service, _registry, _scope = fixture._service(store)
            source_a = fixture._financial_attempt(
                store,
                service,
                suffix="source-a",
            )
            source_b = fixture._financial_attempt(
                store,
                service,
                suffix="source-b",
            )
            pending, _confirmed = self._confirm(store, source_b)

            resolved = resolve_confirmed_pending_source_admission(
                store,
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(resolved.admission_id, source_b.admission_id)
            self.assertNotEqual(resolved.admission_id, source_a.admission_id)

    def test_source_risk_reducing_is_recovered_not_guessed_from_reduce_only(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            fixture = self._fixture()
            service, _registry, _scope = fixture._service(store)
            kwargs = public_financial_kwargs(
                store,
                environment="SIMULATION",
            )
            reservations = DurableReservationBook(
                store,
                environment="SIMULATION",
                account_id="paper-1",
            )
            source = service.admit(
                command_id="proposal-command-risk-reducing",
                idempotency_key="proposal-idem-risk-reducing",
                admission_id="proposal-admission-risk-reducing",
                policy_id="p1",
                intent_id="proposal-intent-risk-reducing",
                account_id="paper-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                action="ORDER.SUBMIT",
                notional="100",
                reservation_book=reservations,
                reservation_id="proposal-reservation-risk-reducing",
                confirmation_id=None,
                risk_reducing=True,
                **kwargs,
            )
            self.assertEqual(source.reason, "confirmation_required")
            self.assertFalse(kwargs["risk_intent"].reduce_only)
            self.assertTrue(source.risk_reducing)
            pending, _confirmed = self._confirm(store, source)

            resolved = resolve_confirmed_pending_source_admission(
                store,
                pending_intent_id=pending.pending_intent_id,
                at=NOW + timedelta(seconds=1),
            )
            self.assertTrue(resolved.risk_reducing)
            self.assertFalse(pending.risk_intent.reduce_only)

    def test_source_resolution_api_exposes_no_financial_or_source_overrides(self) -> None:
        signature = inspect.signature(resolve_confirmed_pending_source_admission)
        self.assertEqual(
            tuple(signature.parameters),
            ("store", "pending_intent_id", "at"),
        )
        for forbidden in (
            "admission_id",
            "risk_reducing",
            "risk_intent",
            "quantity",
            "price",
            "notional",
            "risk_policy",
            "reservation_requirements",
            "account_id",
            "environment",
            "policy_id",
            "confirmation_id",
            "actor_id",
            "allocation_result",
        ):
            self.assertNotIn(forbidden, signature.parameters)


if __name__ == "__main__":
    unittest.main()
