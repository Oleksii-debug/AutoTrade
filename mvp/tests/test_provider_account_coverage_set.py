from datetime import timedelta
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.dispatch import stable_client_order_id
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_account_absence_coverage import (
    issue_provider_account_surface_coverage,
    resolve_historical_unknown_submission,
)
from mvp.autotrade_mvp.provider_account_absence_semantics import (
    resolve_current_provider_account_absence_semantics,
)
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
)
from mvp.autotrade_mvp.provider_account_coverage_set import (
    ProviderAccountCoverageSetError,
    ProviderAccountRequiredSurfaceCoverageSet,
    issue_provider_account_required_surface_coverage_set,
    require_current_provider_account_coverage_set_authority,
    require_provider_account_coverage_set_authority,
)
from mvp.autotrade_mvp.provider_account_origin_set import issue_provider_account_origin_set
from mvp.autotrade_mvp.provider_account_page_chain import issue_provider_account_page_chain
from mvp.autotrade_mvp.provider_account_reconciliation_semantics import (
    resolve_current_provider_account_reconciliation_semantics,
)
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginJournal,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_route_reads import (
    prepare_qualified_provider_read,
    qualified_read_route_semantic_claim,
)
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.tests.provider_qualification_test_support import ExactQualificationProjectionHarness
from mvp.tests.test_provider_account_absence_coverage import (
    ATTEMPT_ID,
    INTENT_ID,
    _append_unknown,
    _milliseconds,
)
from mvp.tests.test_provider_account_page_chain import (
    ProviderAccountPageChainTests,
    _absence_claims,
)
from mvp.tests.test_provider_route_reads import verified_read_capability
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


SURFACE_CONFIG = {
    "OPEN_ORDERS": ("/v5/order/realtime", "ORDER.READ"),
    "ORDER_HISTORY": ("/v5/order/history", "ORDER.READ"),
    "EXECUTIONS": ("/v5/execution/list", "ORDER.READ"),
    "ACTIVITIES": ("/v5/account/transaction-log", "ACCOUNT.READ"),
}


def _claims() -> dict[str, str]:
    claims = _absence_claims()
    for endpoint, permission in SURFACE_CONFIG.values():
        key, digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=Surface.AUTHENTICATED_READ,
            permission_scope=permission,
        )
        claims[key] = digest
    return claims


class ProviderAccountCoverageSetTests(unittest.TestCase):
    def _fixture(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        capabilities = DurableCapabilityRegistry(journal)
        capabilities.add(
            verified_read_capability(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW - timedelta(minutes=1),
                permission_scopes=frozenset(
                    {"ORDER.READ", "ORDER.WRITE", "ACCOUNT.READ"}
                ),
                data_entitlements=frozenset(
                    {"QUOTE", "BALANCES", "ORDERS", "EXECUTIONS", "ACTIVITIES"}
                ),
            )
        )
        evidence_root = Path(directory) / "evidence"
        harness = ExactQualificationProjectionHarness().start()
        self.addCleanup(harness.stop)
        qualifications = harness.registry(
            journal,
            evidence_store=ArtifactStore(evidence_root),
            evidence_root=evidence_root,
        )
        record, receipt, protocol = accepted_spot_q(
            ordinal=197,
            include_read_rule=False,
            extra_route_semantics=_claims(),
        )
        harness.register(protocol_key=protocol.key, record=record, receipt=receipt)
        qualifications._append_accepted(
            protocol_key=protocol.key,
            record=record,
            receipt=receipt,
        )
        selection = select_provider(
            route_request(),
            [candidate()],
            at=NOW,
            capability_registry=capabilities,
            qualification_registry=qualifications,
        )
        self.assertEqual(selection.status, "SELECTED_UNAMBIGUOUS")
        route = selection.selected
        self.assertIsNotNone(route)
        account_id = route.candidate.account_id
        environment = record.scope.provider_scope.runtime_environment
        client_order_id = stable_client_order_id(
            "BYBIT",
            INTENT_ID,
            environment=environment,
            account_id=account_id,
        )
        send_at = NOW - timedelta(minutes=10)
        start_time = _milliseconds(send_at - timedelta(minutes=1))
        end_time = _milliseconds(NOW - timedelta(minutes=1))
        _append_unknown(
            journal,
            environment=environment,
            account_id=account_id,
            provider="BYBIT",
            intent_id=INTENT_ID,
            attempt_id=ATTEMPT_ID,
            client_order_id=client_order_id,
            sent_at=send_at,
        )
        historical = resolve_historical_unknown_submission(
            journal,
            environment=environment,
            account_id=account_id,
            attempt_id=ATTEMPT_ID,
        )
        origin = ProviderOriginJournal(
            journal,
            response_store=ArtifactStore(Path(directory) / "provider-origin-artifacts"),
        )
        acquisition_authority = DurableProviderAccountAcquisitionAuthority(journal)
        acquisition = acquisition_authority.issue_serialized(
            provider_scope=record.scope.provider_scope,
            account_id=account_id,
            acquisition_request_id="coverage-set-acquisition-1",
            committed_at=NOW,
        )
        page_fixture = (
            journal,
            capabilities,
            qualifications,
            route,
            origin,
            acquisition_authority,
            acquisition,
            None,
        )

        queries = {
            "OPEN_ORDERS": {
                "category": "spot",
                "orderLinkId": client_order_id,
                "limit": "50",
            },
            "ORDER_HISTORY": {
                "category": "spot",
                "orderLinkId": client_order_id,
                "startTime": start_time,
                "endTime": end_time,
                "limit": "50",
            },
            "EXECUTIONS": {
                "category": "spot",
                "orderLinkId": client_order_id,
                "startTime": start_time,
                "endTime": end_time,
                "limit": "100",
            },
            "ACTIVITIES": {
                "category": "spot",
                "accountType": "UNIFIED",
                "startTime": start_time,
                "endTime": end_time,
                "limit": "50",
            },
        }
        bindings = {}
        responses = {}
        observations = {}
        for surface, (endpoint, permission) in SURFACE_CONFIG.items():
            binding = prepare_qualified_provider_read(
                route,
                capabilities,
                qualifications,
                surface=Surface.AUTHENTICATED_READ,
                endpoint=endpoint,
                query=queries[surface],
                at=NOW,
                permission_scope=permission,
            )
            response = ProviderAccountPageChainTests._direct_response(
                self,
                page_fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="coverage-set-" + surface.lower(),
            )
            bindings[surface] = binding
            responses[surface] = response
            observations[surface] = observe_provider_origin_json_response(
                response_binding=response,
                query_binding=binding,
            )

        origin_set = issue_provider_account_origin_set(
            qualification_registry=qualifications,
            account_acquisition_authority=acquisition_authority,
            account_acquisition=acquisition,
            response_bindings=tuple(responses[s] for s in sorted(responses)),
            at=NOW,
        )
        reconciliation = resolve_current_provider_account_reconciliation_semantics(
            qualification_registry=qualifications,
            qualification_id=record.qualification_id,
            provider_scope_digest=record.scope.provider_scope.content_digest,
            at=NOW,
        )
        absence = resolve_current_provider_account_absence_semantics(
            reconciliation_semantics=reconciliation,
            qualification_registry=qualifications,
            at=NOW,
        )
        page_chains = {}
        coverages = {}
        for surface in sorted(SURFACE_CONFIG):
            page_chain = issue_provider_account_page_chain(
                absence_semantics=absence,
                origin_set=origin_set,
                observations=(observations[surface],),
                qualification_registry=qualifications,
                surface=surface,
                at=NOW,
            )
            page_chains[surface] = page_chain
            coverages[surface] = issue_provider_account_surface_coverage(
                absence_semantics=absence,
                page_chain=page_chain,
                historical_submission=historical,
                qualification_registry=qualifications,
                at=NOW,
            )
        return (
            qualifications,
            acquisition_authority,
            acquisition,
            origin_set,
            absence,
            historical,
            page_chains,
            coverages,
        )

    def _issue(self, fixture):
        (
            qualifications,
            _acquisition_authority,
            _acquisition,
            origin_set,
            absence,
            historical,
            page_chains,
            coverages,
        ) = fixture
        return issue_provider_account_required_surface_coverage_set(
            coverages=tuple(coverages[s] for s in sorted(coverages)),
            page_chains=tuple(page_chains[s] for s in sorted(page_chains)),
            origin_set=origin_set,
            absence_semantics=absence,
            historical_submission=historical,
            qualification_registry=qualifications,
            at=NOW,
        )

    def test_constructor_is_sealed(self):
        with self.assertRaisesRegex(ProviderAccountCoverageSetError, "canonical issuer"):
            ProviderAccountRequiredSurfaceCoverageSet()

    def test_exact_four_surfaces_issue_current_coverage_set(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            value = self._issue(fixture)
            self.assertEqual(
                tuple(item["surface"] for item in value.surfaces),
                ("ACTIVITIES", "EXECUTIONS", "OPEN_ORDERS", "ORDER_HISTORY"),
            )
            self.assertTrue(
                value.content_digest.startswith(
                    "provider-account-required-coverage-set:sha256:"
                )
            )
            require_provider_account_coverage_set_authority(value)
            self.assertIs(
                require_current_provider_account_coverage_set_authority(
                    value,
                    at=NOW,
                ),
                value,
            )

    def test_missing_or_duplicate_surface_fails_closed(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            qualifications, _, _, origin_set, absence, historical, chains, coverages = fixture
            ordered = sorted(coverages)
            with self.assertRaisesRegex(ProviderAccountCoverageSetError, "exactly the four"):
                issue_provider_account_required_surface_coverage_set(
                    coverages=tuple(coverages[s] for s in ordered[:-1]),
                    page_chains=tuple(chains[s] for s in ordered[:-1]),
                    origin_set=origin_set,
                    absence_semantics=absence,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )
            duplicated = tuple(coverages[s] for s in ordered[:-1]) + (coverages[ordered[0]],)
            with self.assertRaisesRegex(ProviderAccountCoverageSetError, "duplicate"):
                issue_provider_account_required_surface_coverage_set(
                    coverages=duplicated,
                    page_chains=tuple(chains[s] for s in ordered),
                    origin_set=origin_set,
                    absence_semantics=absence,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_acquisition_advance_revokes_current_set(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            value = self._issue(fixture)
            acquisition_authority, acquisition = fixture[1:3]
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="coverage-set-acquisition-2",
                committed_at=NOW,
            )
            require_provider_account_coverage_set_authority(value)
            with self.assertRaisesRegex(
                ProviderAccountCoverageSetError,
                "not exact current authority",
            ):
                require_current_provider_account_coverage_set_authority(
                    value,
                    at=NOW,
                )

    def test_issuer_has_no_completeness_horizon_or_absence_verdict_flags(self):
        parameters = inspect.signature(
            issue_provider_account_required_surface_coverage_set
        ).parameters
        for forbidden in (
            "coverage_complete",
            "pagination_complete",
            "consistency_horizon_satisfied",
            "horizon_elapsed",
            "proven_absent",
            "current",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
