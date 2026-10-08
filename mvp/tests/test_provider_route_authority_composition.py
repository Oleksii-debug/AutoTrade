from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import financial_send_authority, production_host
from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.durable_financial_bybit_sender import (
    DurableFinanciallyBoundBybitOrderSender,
)
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingRegistry,
)
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthorityError,
    FinanciallyBoundBybitOrderSender,
    build_financial_send_authority_issuer,
)
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_bybit import build_production_bybit_order_sender
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_route_dispatch import (
    ProviderRouteDispatchError,
    bind_selected_provider_route_submission_scope,
    compose_selected_provider_route_authority,
)
from mvp.autotrade_mvp.provider_route_financial_binding import (
    build_selected_bybit_transport_authority_inputs,
)
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.tests.test_provider_route_dispatch import (
    ProviderRouteDispatchTests,
    successor_spot_q,
)
from mvp.tests.test_provider_selection import NOW


_AT = NOW.isoformat().replace("+00:00", "Z")


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class SelectedRouteAuthorityCompositionTests(unittest.TestCase):
    def _fixture(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        return fixture.setup_route(directory)

    @staticmethod
    def _compose(journal, capabilities, qualifications, route, authority_check):
        return compose_selected_provider_route_authority(
            store=journal,
            environment="PAPER",
            account_id="paper-account",
            route=route,
            capability_registry=capabilities,
            qualification_registry=qualifications,
            authority_check=authority_check,
        )

    @staticmethod
    def _runtime(directory: str, journal: JournalStore):
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="paper-account",
            environment="PAPER",
            host_id="host-route-authority",
            bind_host="127.0.0.1",
            bind_port=18767,
            public_origin="http://127.0.0.1:18767",
        )
        boundary = object.__new__(SecurityBoundary)
        application = object.__new__(AuthenticatedHostApplication)
        application.security_boundary = boundary
        host = ProductionHostRuntime(
            config=config,
            journal=journal,
            application=application,
            server=object(),
            instance_fence=_FenceStub(),
            admission_gate=object(),
            issuance_token=production_host._RUNTIME_ISSUANCE_TOKEN,
        )
        return compose_financial_authority(host)

    def test_exact_current_c_q_extend_upstream_financial_guard(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(
                journal,
                capabilities,
                qualifications,
                route,
                lambda intent_hash, at: (True, "financial_authority_current"),
            )
            self.assertEqual(
                combined("intent-hash", _AT),
                (True, "financial_authority_current"),
            )

    def test_upstream_financial_rejection_is_not_upgraded_by_current_c_q(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(
                journal,
                capabilities,
                qualifications,
                route,
                lambda intent_hash, at: (False, "financial_risk_rejected"),
            )
            self.assertEqual(
                combined("intent-hash", _AT),
                (False, "financial_risk_rejected"),
            )

    def test_q2_cannot_upgrade_request_bound_to_q1(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, q1, harness = (
                self._fixture(directory)
            )
            combined = self._compose(
                journal,
                capabilities,
                qualifications,
                route,
                lambda intent_hash, at: (True, "financial_authority_current"),
            )
            q2, receipt2, protocol2 = successor_spot_q(
                old_qualification_id=q1.qualification_id
            )
            harness.register(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_accepted(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            self.assertEqual(
                combined("intent-hash", _AT),
                (False, "provider_qualification_not_exact_current"),
            )

    def test_c_q_registries_must_share_exact_financial_journal(self):
        with TemporaryDirectory() as directory:
            journal, _capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            other = JournalStore(Path(directory) / "other.sqlite3")
            capabilities = DurableCapabilityRegistry(other)
            with self.assertRaisesRegex(
                ProviderRouteDispatchError,
                "share one JournalStore instance",
            ):
                self._compose(
                    journal,
                    capabilities,
                    qualifications,
                    route,
                    lambda intent_hash, at: (True, "financial_authority_current"),
                )

    def test_route_submission_scope_persists_exact_c_q_identity(self):
        with TemporaryDirectory() as directory:
            _journal, _capabilities, _qualifications, route, _dispatcher, q1, _harness = (
                self._fixture(directory)
            )
            scope = bind_selected_provider_route_submission_scope(
                route,
                {
                    "provider_id": "BYBIT",
                    "account_id": "paper-account",
                    "environment": "PAPER",
                    "provider_environment": "TESTNET",
                    "capability_snapshot_id": route.capability_snapshot_id,
                },
            )
            self.assertEqual(
                scope["provider_route_qualification_id"],
                q1.qualification_id,
            )
            self.assertEqual(
                scope["provider_route_capability_snapshot_id"],
                route.capability_snapshot_id,
            )
            self.assertEqual(
                scope["provider_route_decision_journal_sequence_cut"],
                route.decision_journal_sequence_cut,
            )

    def test_route_submission_scope_rejects_polymorphic_mapping_before_callbacks(self):
        with TemporaryDirectory() as directory:
            _journal, _capabilities, _qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            touched = []

            class HostileScope(dict):
                def __iter__(self):
                    touched.append("iter")
                    raise AssertionError("unexpected scope iteration")

                def items(self):
                    touched.append("items")
                    raise AssertionError("unexpected scope items")

                def keys(self):
                    touched.append("keys")
                    raise AssertionError("unexpected scope keys")

            with self.assertRaisesRegex(TypeError, "exact dict"):
                bind_selected_provider_route_submission_scope(
                    route,
                    HostileScope({"provider_id": "BYBIT"}),
                )
            self.assertEqual(touched, [])

    def test_route_submission_scope_rejects_reserved_identity_override(self):
        with TemporaryDirectory() as directory:
            _journal, _capabilities, _qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            with self.assertRaisesRegex(
                ProviderRouteDispatchError,
                "attempts to override provider-route authority fields",
            ):
                bind_selected_provider_route_submission_scope(
                    route,
                    {"provider_route_qualification_id": "forged"},
                )

    def test_financial_issuer_can_be_bound_to_exact_selected_route_authority(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
                selected_route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            self.assertTrue(issuer.provider_route_bound)
            self.assertIs(issuer.runtime, runtime)

    def test_selected_route_reaches_public_bybit_product_builder_without_new_authority(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
                selected_route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            transport_inputs = build_selected_bybit_transport_authority_inputs(route)
            sender = build_production_bybit_order_sender(
                runtime,
                financial_issuer=issuer,
                financial_binding_registry=DurableFinancialRequestBindingRegistry(journal),
                **transport_inputs,
                credential_handle=PersistentCredentialHandle(
                    handle_id="cred-route-bybit",
                    account_id="paper-account",
                    provider="BYBIT",
                    environment="PAPER",
                    provider_environment=route.candidate.provider_environment,
                    purpose="TRADE",
                    generation=1,
                ),
                session_token="route-session",
                clock_millis=lambda: 1_700_000_000_000,
                clock_utc=lambda: NOW,
            )
            self.assertIs(type(sender), DurableFinanciallyBoundBybitOrderSender)
            bound = sender._DurableFinanciallyBoundBybitOrderSender__sender
            self.assertIs(type(bound), FinanciallyBoundBybitOrderSender)
            lower = bound._FinanciallyBoundBybitOrderSender__sender
            transport = lower._ProductionBybitOrderSender__transport
            self.assertEqual(
                transport.capability_snapshot_id,
                route.capability_snapshot_id,
            )
            self.assertEqual(
                transport.provider_environment,
                route.candidate.provider_environment,
            )
            self.assertIs(
                transport.capability_registry.require_verified(
                    provider_id="BYBIT",
                    account_id="paper-account",
                    entity_id=route.candidate.entity_id,
                    environment="PAPER",
                    provider_environment=route.candidate.provider_environment,
                    instrument_version=route.capability.instrument_version,
                    at=NOW,
                ),
                route.capability,
            )

    def test_public_bybit_builder_rejects_registry_without_exact_selected_capability(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
                selected_route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "exact selected provider capability",
            ):
                build_production_bybit_order_sender(
                    runtime,
                    financial_issuer=issuer,
                    financial_binding_registry=DurableFinancialRequestBindingRegistry(journal),
                    provider_environment=route.candidate.provider_environment,
                    capability_snapshot_id=route.capability_snapshot_id,
                    capability_registry=CapabilityRegistry(),
                    credential_handle=PersistentCredentialHandle(
                        handle_id="cred-route-bybit-missing-capability",
                        account_id="paper-account",
                        provider="BYBIT",
                        environment="PAPER",
                        provider_environment=route.candidate.provider_environment,
                        purpose="TRADE",
                        generation=1,
                    ),
                    session_token="route-session-missing-capability",
                    clock_millis=lambda: 1_700_000_000_000,
                    clock_utc=lambda: NOW,
                )

    def test_partial_provider_route_binding_is_rejected(self):
        with TemporaryDirectory() as directory:
            journal, _capabilities, _qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "requires selected_route plus durable C/Q registries",
            ):
                build_financial_send_authority_issuer(
                    AuthorityService(journal),
                    runtime,
                    selected_route=route,
                )

    def test_provider_route_issuer_rejects_cross_store_c_q_authority(self):
        with TemporaryDirectory() as directory:
            journal, _capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            other = JournalStore(Path(directory) / "other-issuer.sqlite3")
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "share one exact JournalStore",
            ):
                build_financial_send_authority_issuer(
                    AuthorityService(journal),
                    runtime,
                    selected_route=route,
                    capability_registry=DurableCapabilityRegistry(other),
                    qualification_registry=qualifications,
                )

    def test_route_authority_composer_rebinding_fails_before_use(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
                selected_route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            original = financial_send_authority.compose_selected_provider_route_authority
            calls = []

            def forged(*_args, **_kwargs):
                calls.append("forged")
                raise AssertionError("forged provider route authority executed")

            financial_send_authority.compose_selected_provider_route_authority = forged
            try:
                with self.assertRaisesRegex(
                    FinancialSendAuthorityError,
                    "provider route authority composer changed",
                ):
                    _ = issuer.provider_route_bound
            finally:
                financial_send_authority.compose_selected_provider_route_authority = original
            self.assertEqual(calls, [])

    def test_financial_route_scope_builder_rebinding_fails_before_use(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
                selected_route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            original = (
                financial_send_authority.build_selected_provider_route_financial_submission_scope
            )
            calls = []

            def forged(*_args, **_kwargs):
                calls.append("forged")
                raise AssertionError("forged financial route scope builder executed")

            financial_send_authority.build_selected_provider_route_financial_submission_scope = (
                forged
            )
            try:
                with self.assertRaisesRegex(
                    FinancialSendAuthorityError,
                    "financial provider-route scope authority changed",
                ):
                    _ = issuer.provider_route_bound
            finally:
                financial_send_authority.build_selected_provider_route_financial_submission_scope = (
                    original
                )
            self.assertEqual(calls, [])

    def test_route_submission_scope_rebinding_fails_before_use(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = self._runtime(directory, journal)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
                selected_route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            original = financial_send_authority.bind_selected_provider_route_submission_scope
            calls = []

            def forged(*_args, **_kwargs):
                calls.append("forged")
                raise AssertionError("forged provider route submission scope executed")

            financial_send_authority.bind_selected_provider_route_submission_scope = forged
            try:
                with self.assertRaisesRegex(
                    FinancialSendAuthorityError,
                    "provider route submission scope authority changed",
                ):
                    _ = issuer.provider_route_bound
            finally:
                financial_send_authority.bind_selected_provider_route_submission_scope = original
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
