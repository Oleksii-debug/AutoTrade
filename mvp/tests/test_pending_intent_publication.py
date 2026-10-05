from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import AuthorityPolicy, AuthorityService
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.pending_intent_financial_binding import (
    DurablePendingIntentFinancialBindingRegistry,
)
from mvp.autotrade_mvp.pending_intent_publication import (
    PendingIntentPublicationError,
    publish_confirmation_required_intent,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)
from mvp.tests.test_authority import (
    INSTRUMENT_ID,
    PUBLIC_INTENT_HASH,
    policy,
    public_authoritative_risk_snapshot,
    public_financial_kwargs,
    public_risk_policy,
)


NOW = datetime(2026, 9, 24, 18, 1, 5, tzinfo=timezone.utc)


class PendingIntentPublicationTests(unittest.TestCase):
    @staticmethod
    def _scope() -> RiskPolicyScope:
        return RiskPolicyScope(
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
            provider_environment="SIMULATION",
            entity_policy_id="entity-policy-1",
            instrument_family="SPOT",
        )

    def _service(
        self,
        store: JournalStore,
        *,
        risk_policy=None,
    ) -> tuple[AuthorityService, DurableRiskPolicyRegistry, RiskPolicyScope]:
        selected_policy = public_risk_policy() if risk_policy is None else risk_policy
        scope = self._scope()
        registry = DurableRiskPolicyRegistry(store)
        registry.register(
            scope=scope,
            policy_id="risk-policy-1",
            version=1,
            policy=selected_policy,
            committed_at=datetime(2026, 9, 24, 17, 59, 50, tzinfo=timezone.utc),
        )
        registry.activate(
            scope=scope,
            policy_id="risk-policy-1",
            version=1,
            committed_at=datetime(2026, 9, 24, 17, 59, 51, tzinfo=timezone.utc),
        )

        def resolver(request):
            snapshot = public_authoritative_risk_snapshot(
                request,
                risk_policy=selected_policy,
            )
            return replace(
                snapshot,
                resolved_risk_policy=request.resolved_risk_policy,
                provider_environment=request.provider_environment,
                entity_policy_id=request.entity_policy_id,
                instrument_family=request.instrument_family,
            )

        service = AuthorityService(
            store,
            risk_policy_scope=scope,
            risk_authority_resolver=resolver,
        )
        service.register_policy(
            policy(
                environments={"SIMULATION"},
                version=3,
            )
        )
        return service, registry, scope

    def _financial_attempt(
        self,
        store: JournalStore,
        service: AuthorityService,
        *,
        selected_risk_policy=None,
        suffix: str = "1",
    ):
        kwargs = public_financial_kwargs(
            store,
            environment="SIMULATION",
            **(
                {}
                if selected_risk_policy is None
                else {"risk_policy": selected_risk_policy}
            ),
        )
        reservations = DurableReservationBook(
            store,
            environment="SIMULATION",
            account_id="paper-1",
        )
        return service.admit(
            command_id=f"proposal-command-{suffix}",
            idempotency_key=f"proposal-idem-{suffix}",
            admission_id=f"proposal-admission-{suffix}",
            policy_id="p1",
            intent_id=f"proposal-intent-{suffix}",
            account_id="paper-1",
            environment="SIMULATION",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            action="ORDER.SUBMIT",
            notional="100",
            reservation_book=reservations,
            reservation_id=f"proposal-reservation-{suffix}",
            confirmation_id=None,
            **kwargs,
        )

    def test_confirmation_required_rejection_publishes_server_owned_pending_intent(self) -> None:
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            service, risk_registry, scope = self._service(store)
            record = self._financial_attempt(store, service)

            self.assertEqual(record.outcome, "REJECTED")
            self.assertEqual(record.reason, "confirmation_required")
            self.assertIsNone(record.reservation_id)
            self.assertEqual(record.intent_hash, PUBLIC_INTENT_HASH)

            pending = publish_confirmation_required_intent(
                store,
                admission_id=record.admission_id,
                at=NOW,
            )
            self.assertTrue(pending.pending_intent_id.startswith("pending-intent:sha256:"))
            self.assertEqual(pending.account_id, record.account_id)
            self.assertEqual(pending.environment, record.environment)
            self.assertEqual(pending.policy_id, record.policy_id)
            self.assertEqual(pending.authority_policy_version, record.policy_version)
            self.assertEqual(pending.instrument_id, record.instrument_version.instrument_id)
            self.assertEqual(pending.instrument_version, record.instrument_version.version)
            self.assertEqual(pending.authority_action, record.action)
            self.assertEqual(pending.notional, Decimal("100"))
            self.assertEqual(pending.registered_at, record.admitted_at)
            self.assertEqual(pending.expires_at, record.risk_valid_until)
            self.assertTrue(pending.intent_hash.startswith("sha256:"))

            binding = DurablePendingIntentFinancialBindingRegistry(store).resolve_current(
                pending.pending_intent_id,
                resolved_risk_policy=risk_registry.resolve_current(scope),
                reservation_requirements={"CASH:USD": "100"},
            )
            self.assertEqual(binding.intent_hash, pending.intent_hash)
            self.assertEqual(binding.risk_policy_id, "risk-policy-1")
            self.assertEqual(binding.risk_policy_version, 1)
            self.assertEqual(
                dict(binding.reservation_requirements),
                {"CASH:USD": "100"},
            )

            retry = publish_confirmation_required_intent(
                store,
                admission_id=record.admission_id,
                at=NOW + timedelta(minutes=1),
            )
            self.assertEqual(retry, pending)

            restarted = publish_confirmation_required_intent(
                JournalStore(path),
                admission_id=record.admission_id,
                at=NOW + timedelta(minutes=2),
            )
            self.assertEqual(restarted, pending)

    def test_risk_policy_activation_drift_blocks_publication(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            service, risk_registry, scope = self._service(store)
            record = self._financial_attempt(store, service)
            self.assertEqual(record.reason, "confirmation_required")

            risk_registry.register(
                scope=scope,
                policy_id="risk-policy-2",
                version=2,
                policy=public_risk_policy(max_single_notional="900"),
                committed_at=datetime(2026, 9, 24, 18, 1, 1, tzinfo=timezone.utc),
            )
            risk_registry.activate(
                scope=scope,
                policy_id="risk-policy-2",
                version=2,
                committed_at=datetime(2026, 9, 24, 18, 1, 2, tzinfo=timezone.utc),
            )

            with self.assertRaisesRegex(
                PendingIntentPublicationError,
                "activation changed",
            ):
                publish_confirmation_required_intent(
                    store,
                    admission_id=record.admission_id,
                    at=NOW,
                )

    def test_risk_rejection_cannot_be_promoted_to_confirmation_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            rejecting_policy = public_risk_policy(max_single_notional="50")
            service, _registry, _scope = self._service(
                store,
                risk_policy=rejecting_policy,
            )
            record = self._financial_attempt(
                store,
                service,
                selected_risk_policy=rejecting_policy,
                suffix="risk-rejected",
            )
            self.assertEqual(record.outcome, "REJECTED")
            self.assertEqual(record.reason, "risk_rejected")

            with self.assertRaisesRegex(
                PendingIntentPublicationError,
                "only a confirmation-required",
            ):
                publish_confirmation_required_intent(
                    store,
                    admission_id=record.admission_id,
                    at=NOW,
                )

    def test_publication_api_has_no_client_financial_override_parameters(self) -> None:
        signature = inspect.signature(publish_confirmation_required_intent)
        self.assertEqual(
            tuple(signature.parameters),
            ("store", "admission_id", "at"),
        )
        for forbidden in (
            "risk_intent",
            "quantity",
            "price",
            "notional",
            "risk_policy",
            "reservation_requirements",
            "account_id",
            "environment",
            "policy_id",
        ):
            self.assertNotIn(forbidden, signature.parameters)


if __name__ == "__main__":
    unittest.main()
