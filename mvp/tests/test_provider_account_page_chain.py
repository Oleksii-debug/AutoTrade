from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import inspect
import unittest

from autotrade_runtime.artifacts import ArtifactStore

import mvp.autotrade_mvp.provider_origin as provider_origin_module
import mvp.autotrade_mvp.provider_account_page_chain as page_chain_module
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
    require_current_provider_account_page_chain_authority,
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

        origin = ProviderOriginJournal(
            journal,
            response_store=ArtifactStore(Path(directory) / "provider-origin-artifacts"),
        )
        acquisition_authority = DurableProviderAccountAcquisitionAuthority(journal)
        acquisition = acquisition_authority.issue_serialized(
            provider_scope=record.scope.provider_scope,
            account_id="paper-account",
            acquisition_request_id="page-chain-acquisition-1",
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
        return (
            journal,
            capabilities,
            qualifications,
            route,
            origin,
            acquisition_authority,
            acquisition,
            absence,
        )

    def _binding(self, fixture, *, cursor: str | None = None):
        (
            _journal,
            capabilities,
            qualifications,
            route,
            _origin,
            _acquisition_authority,
            _acquisition,
            _absence,
        ) = fixture
        query = {"category": "spot", "limit": "100"}
        if cursor is not None:
            query["cursor"] = cursor
        return prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query=query,
            at=NOW,
            permission_scope="ORDER.READ",
        )

    def _direct_response(self, fixture, binding, *, body: bytes, marker: str):
        (
            journal,
            _capabilities,
            _qualifications,
            _route,
            origin,
            acquisition_authority,
            acquisition,
            _absence,
        ) = fixture
        attempt_id = origin.prepare_direct(
            binding,
            recorded_at=NOW,
            account_acquisition_authority=acquisition_authority,
            account_acquisition=acquisition,
        )
        prepared = JournalStore.load_events(
            journal,
            "qualified_authenticated_provider_read",
            attempt_id,
        )[0]
        snapshot = provider_origin_module._qualified_query_snapshot(binding)
        observed_at = NOW.isoformat().replace("+00:00", "Z")
        response_sha256 = "sha256:" + sha256(body).hexdigest()
        wire_request_sha256 = "sha256:" + sha256(
            (attempt_id + "|" + marker).encode("utf-8")
        ).hexdigest()
        wire_semantics = (
            provider_origin_module.qualified_authenticated_read_expected_wire_semantics_digest(
                binding.query_binding,
                provider_environment=binding.provider_environment,
            )
        )
        terminal_cut = prepared["journal_sequence"]
        artifact_id = provider_origin_module._response_artifact_id(
            attempt_id=attempt_id,
            qualified_query_digest=snapshot["qualified_query_digest"],
            response_sha256=response_sha256,
        )
        ArtifactStore.publish_bytes(
            origin._response_store,
            artifact_id=artifact_id,
            data=body,
            media_type="application/octet-stream",
            rights={
                "storage": True,
                "export": False,
                "rights_id": "qualified-provider-origin-response:v1",
            },
            source_refs=[],
            metadata={
                "evidence_kind": "QUALIFIED_PROVIDER_ORIGIN_RESPONSE",
                "attempt_id": attempt_id,
                "prepared_subject_digest": prepared["payload_hash"],
                "qualified_query_digest": snapshot["qualified_query_digest"],
                "qualification_id": snapshot["qualification_id"],
                "endpoint_rule_digest": snapshot["endpoint_rule_digest"],
                "qualified_route_rule_digest": snapshot["qualified_route_rule_digest"],
                "data_entitlement": snapshot["data_entitlement"],
                "parser_identity": snapshot["parser_identity"],
                "provider_environment": snapshot["provider_environment"],
                "execution_class": "DIRECT_PROVIDER_WIRE",
                "wire_request_sha256": wire_request_sha256,
                "wire_request_semantics_sha256": wire_semantics,
                "terminal_authority_journal_sequence_cut": terminal_cut,
                "terminal_authority_verified_at": observed_at,
            },
        )
        provider_origin_module._claim_direct_wire_execution(
            journal,
            attempt_id=attempt_id,
            qualified_query_digest=snapshot["qualified_query_digest"],
            qualification_id=snapshot["qualification_id"],
            http_status=200,
            response_sha256=response_sha256,
            observed_at=observed_at,
            wire_request_sha256=wire_request_sha256,
            wire_request_semantics_sha256=wire_semantics,
            terminal_authority_journal_sequence_cut=terminal_cut,
            terminal_authority_verified_at=observed_at,
        )
        return origin.recover_response_binding(attempt_id, binding)

    def _origin_set(self, fixture, responses):
        (
            _journal,
            _capabilities,
            qualifications,
            _route,
            _origin,
            acquisition_authority,
            acquisition,
            _absence,
        ) = fixture
        return issue_provider_account_origin_set(
            qualification_registry=qualifications,
            account_acquisition_authority=acquisition_authority,
            account_acquisition=acquisition,
            response_bindings=tuple(responses),
            at=NOW,
        )

    def _observation(self, response, binding):
        return observe_provider_origin_json_response(
            response_binding=response,
            query_binding=binding,
        )

    def test_single_explicit_terminal_page_issues_sealed_chain(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            binding = self._binding(fixture)
            response = self._direct_response(
                fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="terminal",
            )
            origin_set = self._origin_set(fixture, (response,))
            observation = self._observation(response, binding)
            qualifications = fixture[2]
            absence = fixture[7]
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
            self.assertEqual(len(value.pages), 1)
            self.assertIsNone(value.pages[0]["request_cursor"])
            self.assertEqual(value.pages[0]["response_next_cursor"], "")
            self.assertEqual(value.pages[0]["observed_at"], response.observed_at)
            self.assertEqual(
                value.pages[0]["journal_sequence"],
                response.journal_sequence,
            )
            require_provider_account_page_chain_authority(value)

    def test_current_page_chain_rejects_acquisition_superseded_after_issuance(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            binding = self._binding(fixture)
            response = self._direct_response(
                fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="superseded-after-chain",
            )
            origin_set = self._origin_set(fixture, (response,))
            value = issue_provider_account_page_chain(
                absence_semantics=fixture[7],
                origin_set=origin_set,
                observations=(self._observation(response, binding),),
                qualification_registry=fixture[2],
                surface=SURFACE,
                at=NOW,
            )
            acquisition_authority = fixture[5]
            acquisition = fixture[6]
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="page-chain-acquisition-after-issuance",
                committed_at=NOW,
            )

            require_provider_account_page_chain_authority(value)
            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "not exact current acquisition authority",
            ):
                require_current_provider_account_page_chain_authority(
                    value,
                    at=NOW,
                )

    def test_current_page_chain_ignores_rebound_origin_currentness_helper(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            binding = self._binding(fixture)
            response = self._direct_response(
                fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="rebound-current-origin-helper",
            )
            origin_set = self._origin_set(fixture, (response,))
            value = issue_provider_account_page_chain(
                absence_semantics=fixture[7],
                origin_set=origin_set,
                observations=(self._observation(response, binding),),
                qualification_registry=fixture[2],
                surface=SURFACE,
                at=NOW,
            )
            acquisition_authority = fixture[5]
            acquisition = fixture[6]
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="page-chain-acquisition-rebound-helper",
                committed_at=NOW,
            )

            forged_calls = []
            original = (
                page_chain_module.require_current_provider_account_origin_set_authority
            )

            def forged_current_origin_set_authority(value, *, at):
                forged_calls.append((value, at))
                return value

            page_chain_module.require_current_provider_account_origin_set_authority = (
                forged_current_origin_set_authority
            )
            try:
                with self.assertRaisesRegex(
                    ProviderAccountPageChainError,
                    "not exact current acquisition authority",
                ):
                    require_current_provider_account_page_chain_authority(
                        value,
                        at=NOW,
                    )
            finally:
                page_chain_module.require_current_provider_account_origin_set_authority = (
                    original
                )
            self.assertEqual(forged_calls, [])

    def test_current_page_chain_accepts_no_caller_qualification_registry(self):
        parameters = inspect.signature(
            require_current_provider_account_page_chain_authority
        ).parameters
        self.assertNotIn("qualification_registry", parameters)

    def test_page_chain_rejects_superseded_origin_set_acquisition(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            binding = self._binding(fixture)
            response = self._direct_response(
                fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="superseded-origin-set",
            )
            origin_set = self._origin_set(fixture, (response,))
            observation = self._observation(response, binding)
            acquisition_authority = fixture[5]
            acquisition = fixture[6]
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="page-chain-acquisition-2",
                committed_at=NOW,
            )

            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "origin set is not exact current authority",
            ):
                issue_provider_account_page_chain(
                    absence_semantics=fixture[7],
                    origin_set=origin_set,
                    observations=(observation,),
                    qualification_registry=fixture[2],
                    surface=SURFACE,
                    at=NOW,
                )

    def test_missing_provider_cursor_state_fails_closed(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            binding = self._binding(fixture)
            response = self._direct_response(
                fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[]}}',
                marker="missing-cursor",
            )
            origin_set = self._origin_set(fixture, (response,))
            observation = self._observation(response, binding)
            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "explicit nextPageCursor",
            ):
                issue_provider_account_page_chain(
                    absence_semantics=fixture[7],
                    origin_set=origin_set,
                    observations=(observation,),
                    qualification_registry=fixture[2],
                    surface=SURFACE,
                    at=NOW,
                )

    def test_two_page_chain_order_is_derived_from_provider_cursors(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            root_binding = self._binding(fixture)
            next_binding = self._binding(fixture, cursor="cursor-2")
            root_response = self._direct_response(
                fixture,
                root_binding,
                body=b'{"retCode":0,"result":{"list":[{"execId":"e1"}],"nextPageCursor":"cursor-2"}}',
                marker="root",
            )
            next_response = self._direct_response(
                fixture,
                next_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="second",
            )
            origin_set = self._origin_set(fixture, (root_response, next_response))
            root = self._observation(root_response, root_binding)
            second = self._observation(next_response, next_binding)
            value = issue_provider_account_page_chain(
                absence_semantics=fixture[7],
                origin_set=origin_set,
                observations=(second, root),
                qualification_registry=fixture[2],
                surface=SURFACE,
                at=NOW,
            )
            self.assertEqual(len(value.pages), 2)
            self.assertIsNone(value.pages[0]["request_cursor"])
            self.assertEqual(value.pages[0]["response_next_cursor"], "cursor-2")
            self.assertEqual(value.pages[1]["request_cursor"], "cursor-2")
            self.assertEqual(value.pages[1]["response_next_cursor"], "")

    def test_response_cursor_must_have_exact_next_qualified_page(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            root_binding = self._binding(fixture)
            other_binding = self._binding(fixture, cursor="cursor-other")
            root_response = self._direct_response(
                fixture,
                root_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":"cursor-required"}}',
                marker="root-mismatch",
            )
            other_response = self._direct_response(
                fixture,
                other_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="other",
            )
            origin_set = self._origin_set(fixture, (root_response, other_response))
            observations = (
                self._observation(root_response, root_binding),
                self._observation(other_response, other_binding),
            )
            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "no exact next qualified page",
            ):
                issue_provider_account_page_chain(
                    absence_semantics=fixture[7],
                    origin_set=origin_set,
                    observations=observations,
                    qualification_registry=fixture[2],
                    surface=SURFACE,
                    at=NOW,
                )

    def test_issuer_accepts_no_completion_or_order_booleans(self):
        parameters = inspect.signature(issue_provider_account_page_chain).parameters
        for forbidden in (
            "pagination_complete",
            "consistency_horizon_satisfied",
            "provider_semantics_exclude_execution",
            "page_order",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_constructor_object_new_and_mutation_cannot_forge_authority(self):
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

        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            binding = self._binding(fixture)
            response = self._direct_response(
                fixture,
                binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="mutation",
            )
            value = issue_provider_account_page_chain(
                absence_semantics=fixture[7],
                origin_set=self._origin_set(fixture, (response,)),
                observations=(self._observation(response, binding),),
                qualification_registry=fixture[2],
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
