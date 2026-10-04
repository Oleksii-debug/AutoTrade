from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import financial_send_authority as financial_send_module
from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthorityError,
    FinancialSendAuthorityIssuer,
    build_financial_send_authority_issuer,
)
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.tests.test_financial_send_authority import binding as base_financial_binding
from mvp.tests.test_provider_route_dispatch import (
    ProviderRouteDispatchTests,
    successor_spot_q,
)
from mvp.tests.test_provider_selection import NOW


_AT = NOW.isoformat().replace("+00:00", "Z")


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class FinancialSendProviderRouteAuthorityTests(unittest.TestCase):
    def _route_fixture(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        return fixture.setup_route(directory)

    @staticmethod
    def _runtime(journal: JournalStore):
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="paper-account",
            environment="PAPER",
            host_id="host-route-financial",
            bind_host="127.0.0.1",
            bind_port=18772,
            public_origin="http://127.0.0.1:18772",
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

    @staticmethod
    def _binding_for_route(route):
        provider_scope = route.qualification.scope.provider_scope
        return replace(
            base_financial_binding(),
            provider_scope_digest=provider_scope.content_digest,
            provider_id=route.candidate.provider_id,
            account_id=route.candidate.account_id,
            runtime_environment=provider_scope.runtime_environment,
            provider_environment=route.candidate.provider_environment,
            entity_policy_id=route.candidate.entity_policy_id,
            capability_snapshot_id=route.capability_snapshot_id,
            qualification_identity_digest=route.qualification_id,
        )

    @staticmethod
    def _financial_guard(_service, _admission_id, **_kwargs):
        return lambda _intent_hash, _at: (True, "financial_authority_current")

    @staticmethod
    def _financial_rejection(_service, _admission_id, **_kwargs):
        return lambda _intent_hash, _at: (False, "financial_risk_rejected")

    def _issuer(self, journal, capabilities, qualifications, route, *, guard=None):
        runtime = self._runtime(journal)
        service = AuthorityService(journal)
        guard_function = guard or self._financial_guard
        patcher = patch.object(AuthorityService, "dispatch_guard", new=guard_function)
        patcher.start()
        self.addCleanup(patcher.stop)
        issuer = build_financial_send_authority_issuer(
            service,
            runtime,
            selected_route=route,
            capability_registry=capabilities,
            qualification_registry=qualifications,
        )
        self.assertTrue(issuer.provider_route_bound)
        return issuer

    def _dispatch_material(self, issuer, route):
        material = self._binding_for_route(route)
        capability_material = patch.object(
            FinancialSendAuthorityIssuer,
            "_capability_material",
            return_value=(
                material,
                "admission-route-1",
                "intent-route-1",
                "intent-hash-route-1",
                "ORDER.SUBMIT",
            ),
        )
        durable_admission = patch.object(
            financial_send_module,
            "_require_binding_matches_durable_admission",
            return_value={"intent_id": "intent-route-1"},
        )
        capability_material.start()
        durable_admission.start()
        self.addCleanup(capability_material.stop)
        self.addCleanup(durable_admission.stop)
        return issuer._dispatch_material_for(object())

    def test_product_material_composes_exact_selected_route_c_q(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._route_fixture(directory)
            )
            issuer = self._issuer(journal, capabilities, qualifications, route)
            guard, material, intent_id, intent_hash = self._dispatch_material(issuer, route)

            self.assertEqual(material.qualification_identity_digest, route.qualification_id)
            self.assertEqual(material.capability_snapshot_id, route.capability_snapshot_id)
            self.assertEqual(intent_id, "intent-route-1")
            self.assertEqual(intent_hash, "intent-hash-route-1")
            self.assertEqual(
                guard("intent-hash-route-1", _AT),
                (True, "financial_authority_current"),
            )

    def test_product_material_preserves_upstream_financial_rejection(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._route_fixture(directory)
            )
            issuer = self._issuer(
                journal,
                capabilities,
                qualifications,
                route,
                guard=self._financial_rejection,
            )
            guard, _material, _intent_id, _intent_hash = self._dispatch_material(
                issuer, route
            )
            self.assertEqual(
                guard("intent-hash-route-1", _AT),
                (False, "financial_risk_rejected"),
            )

    def test_product_material_q2_cannot_upgrade_request_bound_to_q1(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, q1, harness = (
                self._route_fixture(directory)
            )
            issuer = self._issuer(journal, capabilities, qualifications, route)
            guard, _material, _intent_id, _intent_hash = self._dispatch_material(
                issuer, route
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
                guard("intent-hash-route-1", _AT),
                (False, "provider_qualification_not_exact_current"),
            )

    def test_product_issuer_cross_store_c_q_is_rejected(self):
        with TemporaryDirectory() as directory:
            journal, _capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._route_fixture(directory)
            )
            runtime = self._runtime(journal)
            other = JournalStore(Path(directory) / "other-financial.sqlite3")
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

    def test_product_issuer_partial_route_authority_is_rejected(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, _qualifications, route, _dispatcher, _q1, _harness = (
                self._route_fixture(directory)
            )
            runtime = self._runtime(journal)
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "requires selected_route plus durable C/Q registries",
            ):
                build_financial_send_authority_issuer(
                    AuthorityService(journal),
                    runtime,
                    selected_route=route,
                    capability_registry=capabilities,
                )

    def test_product_material_without_route_authority_fails_closed(self):
        with TemporaryDirectory() as directory:
            journal, _capabilities, _qualifications, route, _dispatcher, _q1, _harness = (
                self._route_fixture(directory)
            )
            runtime = self._runtime(journal)
            guard_patcher = patch.object(
                AuthorityService,
                "dispatch_guard",
                new=self._financial_guard,
            )
            guard_patcher.start()
            self.addCleanup(guard_patcher.stop)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
            )
            self.assertFalse(issuer.provider_route_bound)
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "requires selected provider route authority",
            ):
                self._dispatch_material(issuer, route)


if __name__ == "__main__":
    unittest.main()
