from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.confirmed_pending_admission import (
    admit_confirmed_pending_intent,
)
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingError,
    DurableFinancialRequestBindingRegistry,
    _admission_journal_sequence,
    _reservation_scope_digest,
    _simulation_account_cut,
    _simulation_qualification_identity,
)
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.financial_request_binding import (
    FinancialRequestBindingMaterial,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope
from mvp.tests.test_confirmed_pending_admission import (
    ConfirmedPendingAdmissionTests,
    NOW,
)


D3 = "sha256:" + "3" * 64
D4 = "sha256:" + "4" * 64
D5 = "sha256:" + "5" * 64
D6 = "sha256:" + "6" * 64
D7 = "sha256:" + "7" * 64
D8 = "sha256:" + "8" * 64
D9 = "sha256:" + "9" * 64
DA = "sha256:" + "a" * 64
DB = "sha256:" + "b" * 64
DC = "sha256:" + "c" * 64
DD = "sha256:" + "d" * 64
DE = "provider-qualification:sha256:" + "e" * 64


class DurableFinancialRequestBindingTests(unittest.TestCase):
    @staticmethod
    def _admission_fixture():
        return ConfirmedPendingAdmissionTests(methodName="runTest")

    def _admitted_case(self, store: JournalStore):
        fixture = self._admission_fixture()
        source, pending, _confirmed, scope = fixture._confirmed_source(store)
        runtime = fixture._runtime(store)
        admitted = admit_confirmed_pending_intent(
            fixture._fresh_service(store, scope),
            pending_intent_id=pending.pending_intent_id,
            at=NOW + timedelta(seconds=1),
            **runtime,
        )
        self.assertEqual(admitted.outcome, "ADMITTED")
        return source, admitted

    def _material(
        self,
        store: JournalStore,
        admitted,
    ) -> FinancialRequestBindingMaterial:
        events = store.load_events("risk_decision", admitted.risk_decision_id)
        self.assertEqual(len(events), 1)
        risk_payload = events[0]["payload"]
        snapshot = risk_payload["authoritative_risk_snapshot"]
        risk_intent = risk_payload["risk_intent"]
        availability = risk_payload["reservation_availability_evidence"]
        book = DurableReservationBook(
            store,
            environment=admitted.environment,
            account_id=admitted.account_id,
        )
        provider_environment = snapshot.get("provider_environment")
        if type(provider_environment) is not str:
            provider_environment = admitted.environment
        entity_policy_id = snapshot.get("entity_policy_id")
        if type(entity_policy_id) is not str:
            entity_policy_id = "test-entity-policy"
        provider_scope = ProviderFinancialScope(
            provider_id=snapshot["provider_id"],
            runtime_environment=admitted.environment,
            provider_environment=provider_environment,
            entity_policy_id=entity_policy_id,
        )
        account_cut_id, account_cut_digest, account_head_sequence = (
            _simulation_account_cut(availability)
        )
        return FinancialRequestBindingMaterial(
            risk_snapshot_id=snapshot["snapshot_id"],
            risk_decision_id=admitted.risk_decision_id,
            admitted_journal_sequence_cut=_admission_journal_sequence(
                store,
                admitted.admission_id,
            ),
            account_cut_id=account_cut_id,
            account_cut_digest=account_cut_digest,
            account_head_journal_sequence=account_head_sequence,
            reservation_id=admitted.reservation_id,
            reservation_scope_digest=_reservation_scope_digest(
                environment=admitted.environment,
                account_id=admitted.account_id,
                scope_id=book.scope_id,
            ),
            reservation_version=book.version,
            reservation_state_digest="sha256:" + book.state_digest,
            provider_scope_digest=provider_scope.content_digest,
            provider_id=snapshot["provider_id"],
            account_id=admitted.account_id,
            runtime_environment=admitted.environment,
            provider_environment=provider_environment,
            entity_policy_id=entity_policy_id,
            instrument_id=admitted.instrument_version.instrument_id,
            instrument_version=admitted.instrument_version.version,
            quantity_unit="BASE",
            equivalent_exposure_digest=D3,
            capability_snapshot_id=admitted.capability_snapshot_id,
            qualification_identity_digest=_simulation_qualification_identity(
                provider_scope,
                admitted.capability_snapshot_id,
            ),
            client_order_id="confirmed-client-order-1",
            side=risk_intent["side"],
            quantity=risk_intent["quantity"],
            price=risk_intent["price"],
            price_semantics_digest=D4,
            order_type="LIMIT",
            time_in_force="GTC",
            reduce_only=risk_intent["reduce_only"],
            trigger_protection_digest=D5,
            endpoint="/orders",
            query_sha256=D6,
            body_sha256=D7,
            request_sha256=D8,
            submission_scope_digest=D9,
        )

    def _bind(self, registry, *, admission_id, material, seconds=2):
        return registry.bind(
            admission_id=admission_id,
            material=material,
            bound_at=(NOW + timedelta(seconds=seconds)).isoformat().replace(
                "+00:00", "Z"
            ),
        )

    def test_restart_recovers_exact_admitted_provider_request_identity(self) -> None:
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            _source, admitted = self._admitted_case(store)
            material = self._material(store, admitted)

            bound = self._bind(
                DurableFinancialRequestBindingRegistry(store),
                admission_id=admitted.admission_id,
                material=material,
            )
            self.assertEqual(bound, material)

            restarted = DurableFinancialRequestBindingRegistry(JournalStore(path))
            self.assertEqual(restarted.resolve(admitted.admission_id), material)

    def test_exact_duplicate_bind_is_idempotent_across_later_observation_time(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            _source, admitted = self._admitted_case(store)
            material = self._material(store, admitted)
            registry = DurableFinancialRequestBindingRegistry(store)

            first = self._bind(
                registry,
                admission_id=admitted.admission_id,
                material=material,
                seconds=2,
            )
            first_sequence = store.current_journal_sequence()
            second = self._bind(
                registry,
                admission_id=admitted.admission_id,
                material=material,
                seconds=30,
            )
            self.assertEqual(first, second)
            self.assertEqual(first.binding_id, material.binding_id)
            self.assertEqual(store.current_journal_sequence(), first_sequence)

    def test_binding_event_uses_binding_time_not_historical_admission_time(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            _source, admitted = self._admitted_case(store)
            material = self._material(store, admitted)
            bound_at = (NOW + timedelta(seconds=7)).isoformat().replace(
                "+00:00", "Z"
            )
            DurableFinancialRequestBindingRegistry(store).bind(
                admission_id=admitted.admission_id,
                material=material,
                bound_at=bound_at,
            )
            events = store.load_events(
                "admitted_financial_request_binding",
                admitted.admission_id,
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["committed_at"], bound_at)
            self.assertNotEqual(events[0]["committed_at"], admitted.admitted_at)

    def test_one_admission_cannot_collapse_distinct_execution_semantics(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            _source, admitted = self._admitted_case(store)
            original = self._material(store, admitted)
            registry = DurableFinancialRequestBindingRegistry(store)
            self._bind(
                registry,
                admission_id=admitted.admission_id,
                material=original,
            )
            alternative = replace(
                original,
                order_type="MARKET",
                time_in_force="IOC",
                price=None,
                price_semantics_digest=DA,
                request_sha256=DB,
            )
            self.assertNotEqual(alternative.binding_id, original.binding_id)

            with self.assertRaisesRegex(
                DurableFinancialRequestBindingError,
                "conflicts with durable state",
            ):
                self._bind(
                    registry,
                    admission_id=admitted.admission_id,
                    material=alternative,
                )
            self.assertEqual(registry.resolve(admitted.admission_id), original)

    def test_rejected_confirmation_required_record_cannot_own_request_binding(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            source, admitted = self._admitted_case(store)
            self.assertEqual(source.outcome, "REJECTED")
            material = self._material(store, admitted)

            with self.assertRaisesRegex(
                DurableFinancialRequestBindingError,
                "only an ADMITTED financial record",
            ):
                self._bind(
                    DurableFinancialRequestBindingRegistry(store),
                    admission_id=source.admission_id,
                    material=material,
                )

    def test_shared_financial_authority_axes_must_match_durable_admission(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            _source, admitted = self._admitted_case(store)
            material = self._material(store, admitted)
            registry = DurableFinancialRequestBindingRegistry(store)
            cases = (
                ("admitted_journal_sequence_cut", material.admitted_journal_sequence_cut - 1),
                ("account_cut_id", "provider-account-cut:sha256:" + "0" * 64),
                ("account_cut_digest", DC),
                ("account_head_journal_sequence", 0),
                ("reservation_id", "other-reservation"),
                ("reservation_scope_digest", DD),
                ("reservation_version", material.reservation_version + 1),
                ("reservation_state_digest", DC),
                (
                    "provider_scope_digest",
                    "provider-financial-scope:sha256:" + "0" * 64,
                ),
                ("capability_snapshot_id", "other-capability"),
                ("qualification_identity_digest", DE),
                ("account_id", "other-account"),
                (
                    "instrument_id",
                    "00000000-0000-0000-0000-000000000999",
                ),
                ("quantity", "999"),
                ("reduce_only", not material.reduce_only),
            )
            for field_name, changed in cases:
                with self.subTest(field=field_name):
                    with self.assertRaisesRegex(
                        DurableFinancialRequestBindingError,
                        "differs from durable admitted authority",
                    ):
                        self._bind(
                            registry,
                            admission_id=admitted.admission_id,
                            material=replace(
                                material,
                                **{field_name: changed},
                            ),
                        )

    def test_registry_surface_cannot_send_or_recompose_provider_request(self) -> None:
        bind_parameters = inspect.signature(
            DurableFinancialRequestBindingRegistry.bind
        ).parameters
        resolve_parameters = inspect.signature(
            DurableFinancialRequestBindingRegistry.resolve
        ).parameters
        for forbidden in (
            "sender",
            "transport",
            "adapter",
            "dispatch",
            "order_type",
            "time_in_force",
            "price",
            "quantity",
            "provider_id",
            "request_sha256",
        ):
            self.assertNotIn(forbidden, bind_parameters)
            self.assertNotIn(forbidden, resolve_parameters)


if __name__ == "__main__":
    unittest.main()
