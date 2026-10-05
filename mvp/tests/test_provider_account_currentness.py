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
    require_provider_account_surface_coverage_authority,
    resolve_historical_unknown_submission,
)
from mvp.autotrade_mvp.provider_account_absence_semantics import (
    resolve_current_provider_account_absence_semantics,
)
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
)
from mvp.autotrade_mvp.provider_account_currentness import (
    ProviderAccountCurrentnessError,
    require_current_provider_account_page_chain_consumption,
    require_current_provider_account_surface_coverage_consumption,
)
from mvp.autotrade_mvp.provider_account_origin_set import (
    issue_provider_account_origin_set,
)
from mvp.autotrade_mvp.provider_account_page_chain import (
    issue_provider_account_page_chain,
    require_provider_account_page_chain_authority,
)
from mvp.autotrade_mvp.provider_account_reconciliation_semantics import (
    resolve_current_provider_account_reconciliation_semantics,
)
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginJournal,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_route_reads import prepare_qualified_provider_read
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.tests.provider_qualification_test_support import ExactQualificationProjectionHarness
from mvp.tests.test_provider_account_absence_coverage import (
    ATTEMPT_ID,
    INTENT_ID,
    _append_unknown,
    _milliseconds,
)
from mvp.tests.test_provider_account_origin_set import ProviderAccountOriginSetTests
from mvp.tests.test_provider_account_page_chain import ENDPOINT, SURFACE, _absence_claims
from mvp.tests.test_provider_route_reads import verified_read_capability
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


class ProviderAccountCurrentnessTests(unittest.TestCase):
    def _fixture(self, directory: str, *, ordinal: int = 195):
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
            ordinal=ordinal,
            include_read_rule=False,
            extra_route_semantics=_absence_claims(),
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

        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query={
                "category": "spot",
                "orderLinkId": client_order_id,
                "startTime": _milliseconds(send_at - timedelta(minutes=1)),
                "endTime": _milliseconds(NOW - timedelta(minutes=1)),
                "limit": "100",
            },
            at=NOW,
            permission_scope="ORDER.READ",
        )
        origin = ProviderOriginJournal(
            journal,
            response_store=ArtifactStore(Path(directory) / "provider-origin-artifacts"),
        )
        acquisition_authority = DurableProviderAccountAcquisitionAuthority(journal)
        acquisition = acquisition_authority.issue_serialized(
            provider_scope=record.scope.provider_scope,
            account_id=account_id,
            acquisition_request_id="currentness-acquisition-1",
            committed_at=NOW,
        )
        origin_fixture = (
            None,
            journal,
            origin,
            qualifications,
            acquisition_authority,
            acquisition,
            binding,
        )
        response = ProviderAccountOriginSetTests._direct_binding(
            self,
            origin_fixture,
            directory,
            marker="currentness-root",
        )
        observation = observe_provider_origin_json_response(
            response_binding=response,
            query_binding=binding,
        )
        origin_set = issue_provider_account_origin_set(
            qualification_registry=qualifications,
            account_acquisition_authority=acquisition_authority,
            account_acquisition=acquisition,
            response_bindings=(response,),
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
        page_chain = issue_provider_account_page_chain(
            absence_semantics=absence,
            origin_set=origin_set,
            observations=(observation,),
            qualification_registry=qualifications,
            surface=SURFACE,
            at=NOW,
        )
        coverage = issue_provider_account_surface_coverage(
            absence_semantics=absence,
            page_chain=page_chain,
            historical_submission=historical,
            qualification_registry=qualifications,
            at=NOW,
        )
        return (
            journal,
            qualifications,
            acquisition_authority,
            acquisition,
            origin_set,
            absence,
            page_chain,
            historical,
            coverage,
        )

    def test_exact_current_sources_are_admitted_without_caller_flags(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _journal,
                qualifications,
                _acquisition_authority,
                _acquisition,
                origin_set,
                absence,
                page_chain,
                historical,
                coverage,
            ) = fixture
            self.assertIs(
                require_current_provider_account_page_chain_consumption(
                    page_chain=page_chain,
                    origin_set=origin_set,
                    absence_semantics=absence,
                    qualification_registry=qualifications,
                    at=NOW,
                ),
                page_chain,
            )
            self.assertIs(
                require_current_provider_account_surface_coverage_consumption(
                    coverage=coverage,
                    page_chain=page_chain,
                    origin_set=origin_set,
                    absence_semantics=absence,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                ),
                coverage,
            )

    def test_new_acquisition_revokes_current_consumption_but_not_history(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _journal,
                qualifications,
                acquisition_authority,
                acquisition,
                origin_set,
                absence,
                page_chain,
                historical,
                coverage,
            ) = fixture
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="currentness-acquisition-2",
                committed_at=NOW,
            )
            require_provider_account_page_chain_authority(page_chain)
            require_provider_account_surface_coverage_authority(coverage)
            with self.assertRaisesRegex(
                ProviderAccountCurrentnessError,
                "current acquisition authority",
            ):
                require_current_provider_account_surface_coverage_consumption(
                    coverage=coverage,
                    page_chain=page_chain,
                    origin_set=origin_set,
                    absence_semantics=absence,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_q_expiry_revokes_current_consumption(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            (
                _journal,
                qualifications,
                _acquisition_authority,
                _acquisition,
                origin_set,
                absence,
                page_chain,
                historical,
                coverage,
            ) = fixture
            with self.assertRaises(ProviderAccountCurrentnessError):
                require_current_provider_account_surface_coverage_consumption(
                    coverage=coverage,
                    page_chain=page_chain,
                    origin_set=origin_set,
                    absence_semantics=absence,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW + timedelta(days=2),
                )

    def test_cross_evidence_cannot_be_composed(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            one = self._fixture(first, ordinal=195)
            two = self._fixture(second, ordinal=196)
            with self.assertRaises(ProviderAccountCurrentnessError):
                require_current_provider_account_surface_coverage_consumption(
                    coverage=one[8],
                    page_chain=two[6],
                    origin_set=two[4],
                    absence_semantics=two[5],
                    historical_submission=one[7],
                    qualification_registry=two[1],
                    at=NOW,
                )

    def test_currentness_api_has_no_caller_verdict_or_horizon_inputs(self):
        parameters = inspect.signature(
            require_current_provider_account_surface_coverage_consumption
        ).parameters
        for forbidden in (
            "current",
            "is_current",
            "pagination_complete",
            "consistency_horizon_satisfied",
            "proven_absent",
            "coverage_complete",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
