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
from mvp.autotrade_mvp.provider_account_absence_coverage_set import (
    ProviderAccountAbsenceCoverageSet,
    ProviderAccountAbsenceCoverageSetError,
    issue_provider_account_absence_coverage_set,
    require_provider_account_absence_coverage_set_authority,
)
from mvp.autotrade_mvp.provider_account_absence_semantics import (
    resolve_current_provider_account_absence_semantics,
)
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
)
from mvp.autotrade_mvp.provider_account_origin_set import (
    issue_provider_account_origin_set,
)
from mvp.autotrade_mvp.provider_account_page_chain import (
    issue_provider_account_page_chain,
)
from mvp.autotrade_mvp.provider_account_reconciliation_semantics import (
    resolve_current_provider_account_reconciliation_semantics,
)
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginJournal,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_route_reads import (
    prepare_qualified_provider_read,
    qualified_read_route_semantic_claim,
)
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.autotrade_mvp.provider_transport import (
    BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS,
)
from mvp.tests.provider_qualification_test_support import (
    ExactQualificationProjectionHarness,
)
from mvp.tests.test_provider_account_absence_coverage import (
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


_SURFACES = {
    "OPEN_ORDERS": "/v5/order/realtime",
    "ORDER_HISTORY": "/v5/order/history",
    "EXECUTIONS": "/v5/execution/list",
    "ACTIVITIES": "/v5/account/transaction-log",
}


def _all_surface_claims() -> dict[str, str]:
    claims = _absence_claims()
    for endpoint in _SURFACES.values():
        rule = BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS[endpoint]
        key, digest = qualified_read_route_semantic_claim(
            provider_id="BYBIT",
            endpoint=endpoint,
            surface=rule.surface,
            permission_scope=rule.permission_scope,
        )
        claims[key] = digest
    return claims


class ProviderAccountAbsenceCoverageSetTests(unittest.TestCase):
    def _coverage_graph(
        self,
        directory: str,
        *,
        suffix: str = "a",
    ):
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
            ordinal=95,
            include_read_rule=False,
            extra_route_semantics=_all_surface_claims(),
        )
        harness.register(
            protocol_key=protocol.key,
            record=record,
            receipt=receipt,
        )
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
        runtime_environment = record.scope.provider_scope.runtime_environment

        intent_id = f"coverage-set-intent-{suffix}"
        attempt_id = f"coverage-set-attempt-{suffix}"
        client_order_id = stable_client_order_id(
            "BYBIT",
            intent_id,
            environment=runtime_environment,
            account_id=account_id,
        )
        send_at = NOW - timedelta(minutes=10)
        _append_unknown(
            journal,
            environment=runtime_environment,
            account_id=account_id,
            provider="BYBIT",
            intent_id=intent_id,
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            sent_at=send_at,
        )
        historical = resolve_historical_unknown_submission(
            journal,
            environment=runtime_environment,
            account_id=account_id,
            attempt_id=attempt_id,
        )

        origin = ProviderOriginJournal(
            journal,
            response_store=ArtifactStore(
                Path(directory) / "provider-origin-artifacts"
            ),
        )
        acquisition_authority = DurableProviderAccountAcquisitionAuthority(journal)
        acquisition = acquisition_authority.issue_serialized(
            provider_scope=record.scope.provider_scope,
            account_id=account_id,
            acquisition_request_id=f"coverage-set-acquisition-{suffix}",
            committed_at=NOW,
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
        page_fixture = (
            journal,
            capabilities,
            qualifications,
            route,
            origin,
            acquisition_authority,
            acquisition,
            absence,
        )

        historical_query = {
            "category": "spot",
            "orderLinkId": client_order_id,
            "startTime": _milliseconds(send_at - timedelta(minutes=1)),
            "endTime": _milliseconds(NOW - timedelta(minutes=1)),
        }
        queries = {
            "OPEN_ORDERS": {
                "category": "spot",
                "orderLinkId": client_order_id,
                "limit": "50",
            },
            "ORDER_HISTORY": {
                **historical_query,
                "limit": "50",
            },
            "EXECUTIONS": {
                **historical_query,
                "limit": "100",
            },
            "ACTIVITIES": {
                "accountType": "UNIFIED",
                "category": "spot",
                "startTime": historical_query["startTime"],
                "endTime": historical_query["endTime"],
                "limit": "50",
            },
        }

        coverages = []
        for surface in (
            "OPEN_ORDERS",
            "ORDER_HISTORY",
            "EXECUTIONS",
            "ACTIVITIES",
        ):
            endpoint = _SURFACES[surface]
            rule = BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS[endpoint]
            binding = prepare_qualified_provider_read(
                route,
                capabilities,
                qualifications,
                surface=rule.surface,
                endpoint=endpoint,
                query=queries[surface],
                at=NOW,
                permission_scope=rule.permission_scope,
            )
            response = ProviderAccountPageChainTests._direct_response(
                self,
                page_fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker=f"coverage-set-{suffix}-{surface.lower()}",
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
            page_chain = issue_provider_account_page_chain(
                absence_semantics=absence,
                origin_set=origin_set,
                observations=(observation,),
                qualification_registry=qualifications,
                surface=surface,
                at=NOW,
            )
            coverages.append(
                issue_provider_account_surface_coverage(
                    absence_semantics=absence,
                    page_chain=page_chain,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )
            )
        return tuple(coverages)

    def test_exact_four_surface_graph_issues_sealed_set(self):
        with TemporaryDirectory() as directory:
            coverages = self._coverage_graph(directory)
            value = issue_provider_account_absence_coverage_set(
                coverages=coverages,
            )
            self.assertIsInstance(value, ProviderAccountAbsenceCoverageSet)
            self.assertEqual(
                tuple(item["surface"] for item in value.coverages),
                ("ACTIVITIES", "EXECUTIONS", "OPEN_ORDERS", "ORDER_HISTORY"),
            )
            self.assertEqual(
                value.consistency_horizon_rule_id,
                "BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
            )
            self.assertNotIn("horizon_satisfied", value.payload())
            self.assertTrue(
                value.content_digest.startswith(
                    "provider-account-absence-coverage-set:sha256:"
                )
            )
            require_provider_account_absence_coverage_set_authority(value)

    def test_input_order_does_not_change_content_identity(self):
        with TemporaryDirectory() as directory:
            coverages = self._coverage_graph(directory)
            forward = issue_provider_account_absence_coverage_set(
                coverages=coverages,
            )
            reverse = issue_provider_account_absence_coverage_set(
                coverages=tuple(reversed(coverages)),
            )
            self.assertEqual(forward.content_digest, reverse.content_digest)

    def test_missing_surface_cannot_issue_complete_graph(self):
        with TemporaryDirectory() as directory:
            coverages = self._coverage_graph(directory)
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageSetError,
                "exactly four",
            ):
                issue_provider_account_absence_coverage_set(
                    coverages=coverages[:-1],
                )

    def test_duplicate_surface_cannot_replace_required_surface(self):
        with TemporaryDirectory() as directory:
            coverages = self._coverage_graph(directory)
            by_surface = {item.surface: item for item in coverages}
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageSetError,
                "duplicate provider surface",
            ):
                issue_provider_account_absence_coverage_set(
                    coverages=(
                        by_surface["OPEN_ORDERS"],
                        by_surface["ORDER_HISTORY"],
                        by_surface["EXECUTIONS"],
                        by_surface["EXECUTIONS"],
                    ),
                )

    def test_mixed_historical_submission_graphs_fail_closed(self):
        with TemporaryDirectory() as directory:
            first = self._coverage_graph(str(Path(directory) / "first"), suffix="a")
            second = self._coverage_graph(str(Path(directory) / "second"), suffix="b")
            first_by_surface = {item.surface: item for item in first}
            second_by_surface = {item.surface: item for item in second}
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageSetError,
                "historical_submission_digest differs",
            ):
                issue_provider_account_absence_coverage_set(
                    coverages=(
                        first_by_surface["OPEN_ORDERS"],
                        first_by_surface["ORDER_HISTORY"],
                        first_by_surface["EXECUTIONS"],
                        second_by_surface["ACTIVITIES"],
                    ),
                )

    def test_constructor_object_new_and_source_mutation_cannot_forge_authority(self):
        with self.assertRaisesRegex(
            ProviderAccountAbsenceCoverageSetError,
            "canonical all-surface issuer",
        ):
            ProviderAccountAbsenceCoverageSet()

        forged = object.__new__(ProviderAccountAbsenceCoverageSet)
        with self.assertRaisesRegex(
            ProviderAccountAbsenceCoverageSetError,
            "construction authority",
        ):
            require_provider_account_absence_coverage_set_authority(forged)

        with TemporaryDirectory() as directory:
            coverages = self._coverage_graph(directory)
            value = issue_provider_account_absence_coverage_set(
                coverages=coverages,
            )
            source = coverages[0]
            original = source.page_count
            object.__setattr__(source, "page_count", original + 1)
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageSetError,
                "source authority changed",
            ):
                require_provider_account_absence_coverage_set_authority(value)

    def test_issuer_exposes_no_horizon_or_absence_verdict_flags(self):
        parameters = inspect.signature(
            issue_provider_account_absence_coverage_set
        ).parameters
        self.assertEqual(tuple(parameters), ("coverages",))
        for forbidden in (
            "consistency_horizon_satisfied",
            "provider_semantics_exclude_execution",
            "pagination_complete",
            "proven_absent",
            "release_reservation",
            "at",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
