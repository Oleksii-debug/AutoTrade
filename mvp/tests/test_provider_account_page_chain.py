from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import inspect
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_account_absence_semantics import (
    account_reconciliation_absence_route_semantic,
    resolve_current_provider_account_absence_semantics,
)
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
)
from mvp.autotrade_mvp.provider_account_origin_set import issue_provider_account_origin_set
from mvp.autotrade_mvp.provider_account_page_chain import (
    ProviderAccountPageChain,
    ProviderAccountPageChainError,
    issue_provider_account_page_chain,
    require_provider_account_page_chain_authority,
)
from mvp.autotrade_mvp.provider_account_reconciliation_semantics import (
    account_reconciliation_route_semantics,
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
from mvp.tests.test_provider_account_origin_set import ProviderAccountOriginSetTests
from mvp.tests.test_provider_route_reads import verified_read_capability
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


ENDPOINT = "/v5/execution/list"
SURFACE = "EXECUTIONS"


def _absence_claims() -> dict[str, str]:
    rules = {
        "OPEN_ORDERS": ("/v5/order/realtime", "ORDERS", "BYBIT_OPEN_ORDER_RETENTION_V1"),
        "ORDER_HISTORY": ("/v5/order/history", "ORDERS", "BYBIT_ORDER_HISTORY_RETENTION_V1"),
        "EXECUTIONS": (ENDPOINT, "EXECUTIONS", "BYBIT_EXECUTION_RETENTION_V1"),
        "ACTIVITIES": (
            "/v5/account/transaction-log",
            "ACTIVITIES",
            "BYBIT_ACTIVITY_RETENTION_V1",
        ),
    }
    claims = account_reconciliation_route_semantics(
        acquisition_mode="SERIALIZED_ACQUISITION_GENERATION",
        consistency_method_id="SERIALIZED_SNAPSHOT_READBACK",
        consistency_method_version=1,
    )
    for surface, (endpoint, entitlement, retention) in rules.items():
        claims.update(
            account_reconciliation_absence_route_semantic(
                surface=surface,
                endpoint=endpoint,
                data_entitlement=entitlement,
                query_scope_rule_id="BYBIT_SPOT_ACCOUNT_QUERY_V1",
                pagination_rule_id="BYBIT_V5_CURSOR_V1",
                retention_rule_id=retention,
                consistency_horizon_rule_id="BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
                semantics_version=1,
            )
        )
    key, digest = qualified_read_route_semantic_claim(
        provider_id="BYBIT",
        endpoint=ENDPOINT,
        surface=Surface.AUTHENTICATED_READ,
        permission_scope="ORDER.READ",
    )
    claims[key] = digest
    return claims


class ProviderAccountPageChainTests(unittest.TestCase):
    def _fixture(self, directory: str):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        capabilities = DurableCapabilityRegistry(journal)
        capabilities.add(
            verified_read_capability(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW - timedelta(minutes=1),
                permission_scopes=frozenset({"ORDER.READ", "ORDER.WRITE", "ACCOUNT.READ"}),
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
            ordinal=93,
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

        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query={"category": "spot", "limit": "100"},
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
            account_id=binding.query_binding.account_id,
            acquisition_request_id="page-chain-acquisition-1",
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
            marker="page-chain-root",
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
        return qualifications, origin_set, observation, absence

    def test_single_terminal_provider_page_issues_sealed_chain_without_boolean(self):
        with TemporaryDirectory() as directory:
            qualifications, origin_set, observation, absence = self._fixture(directory)
            value = issue_provider_account_page_chain(
                absence_semantics=absence,
                origin_set=origin_set,
                observations=(observation,),
                qualification_registry=qualifications,
                surface=SURFACE,
                at=NOW,
            )
            self.assertIsInstance(value, ProviderAccountPageChain)
            self.assertEqual(value.surface, SURFACE)
            self.assertEqual(value.endpoint, ENDPOINT)
            self.assertEqual(value.pagination_rule_id, "BYBIT_V5_CURSOR_V1")
            self.assertEqual(len(value.pages), 1)
            self.assertIsNone(value.pages[0]["request_cursor"])
            self.assertEqual(value.pages[0]["response_next_cursor"], "")
            self.assertTrue(
                value.content_digest.startswith("provider-account-page-chain:sha256:")
            )
            require_provider_account_page_chain_authority(value)

    def test_issuer_accepts_no_pagination_complete_or_page_order_boolean(self):
        parameters = inspect.signature(issue_provider_account_page_chain).parameters
        for forbidden in (
            "pagination_complete",
            "consistency_horizon_satisfied",
            "provider_semantics_exclude_execution",
            "page_order",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_origin_set_endpoint_pages_must_be_exactly_observed(self):
        with TemporaryDirectory() as directory:
            qualifications, origin_set, _observation, absence = self._fixture(directory)
            with self.assertRaisesRegex(ProviderAccountPageChainError, "non-empty exact tuple"):
                issue_provider_account_page_chain(
                    absence_semantics=absence,
                    origin_set=origin_set,
                    observations=(),
                    qualification_registry=qualifications,
                    surface=SURFACE,
                    at=NOW,
                )

    def test_constructor_and_object_new_cannot_forge_page_chain_authority(self):
        with self.assertRaisesRegex(ProviderAccountPageChainError, "canonical issuer"):
            ProviderAccountPageChain()
        forged = object.__new__(ProviderAccountPageChain)
        for name, value in {
            "provider_scope_digest": "provider-financial-scope:sha256:" + "0" * 64,
            "account_id": "paper-account",
            "acquisition_id": "provider-account-acquisition:sha256:" + "0" * 64,
            "acquisition_generation": 1,
            "qualification_id": "provider-qualification:sha256:" + "0" * 64,
            "absence_semantics_digest": "provider-account-absence-semantics:sha256:" + "0" * 64,
            "origin_set_digest": "sha256:" + "0" * 64,
            "surface": SURFACE,
            "endpoint": ENDPOINT,
            "data_entitlement": "EXECUTIONS",
            "query_scope_rule_id": "BYBIT_SPOT_ACCOUNT_QUERY_V1",
            "pagination_rule_id": "BYBIT_V5_CURSOR_V1",
            "root_query_json": "{}",
            "pages_json": "[]",
        }.items():
            object.__setattr__(forged, name, value)
        with self.assertRaisesRegex(
            ProviderAccountPageChainError,
            "construction authority is unavailable",
        ):
            require_provider_account_page_chain_authority(forged)

    def test_post_issue_mutation_invalidates_chain_authority(self):
        with TemporaryDirectory() as directory:
            qualifications, origin_set, observation, absence = self._fixture(directory)
            value = issue_provider_account_page_chain(
                absence_semantics=absence,
                origin_set=origin_set,
                observations=(observation,),
                qualification_registry=qualifications,
                surface=SURFACE,
                at=NOW,
            )
            object.__setattr__(value, "pages_json", "[]")
            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "changed after issuance",
            ):
                require_provider_account_page_chain_authority(value)


if __name__ == "__main__":
    unittest.main()
