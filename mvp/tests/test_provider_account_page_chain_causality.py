from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

import mvp.autotrade_mvp.provider_origin as provider_origin_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_account_page_chain import (
    ProviderAccountPageChainError,
    issue_provider_account_page_chain,
)
from mvp.tests.test_provider_account_page_chain import (
    NOW,
    SURFACE,
    ProviderAccountPageChainTests,
)


class ProviderAccountPageChainCausalityTests(unittest.TestCase):
    def helper(self) -> ProviderAccountPageChainTests:
        helper = ProviderAccountPageChainTests(
            methodName="test_single_explicit_terminal_page_issues_sealed_chain"
        )
        self.addCleanup(helper.doCleanups)
        return helper

    def _complete_prepared_response(
        self,
        fixture,
        binding,
        attempt_id: str,
        *,
        body: bytes,
        marker: str,
    ):
        journal = fixture[0]
        origin = fixture[4]
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
        terminal_cut = journal.whole_store_state_cut()["journal_sequence"]
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

    def test_guessed_next_cursor_observed_before_root_cannot_be_stitched(self):
        helper = self.helper()
        with TemporaryDirectory() as directory:
            fixture = helper._fixture(directory)
            root_binding = helper._binding(fixture)
            guessed_binding = helper._binding(fixture, cursor="cursor-2")
            guessed_response = helper._direct_response(
                fixture,
                guessed_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="guessed-before-root",
            )
            root_response = helper._direct_response(
                fixture,
                root_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":"cursor-2"}}',
                marker="root-after-guessed",
            )
            origin_set = helper._origin_set(fixture, (root_response, guessed_response))
            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "durable causal order",
            ):
                issue_provider_account_page_chain(
                    absence_semantics=fixture[7],
                    origin_set=origin_set,
                    observations=(
                        helper._observation(root_response, root_binding),
                        helper._observation(guessed_response, guessed_binding),
                    ),
                    qualification_registry=fixture[2],
                    surface=SURFACE,
                    at=NOW,
                )

    def test_guessed_next_request_prepared_before_root_but_observed_later_fails(self):
        helper = self.helper()
        with TemporaryDirectory() as directory:
            fixture = helper._fixture(directory)
            root_binding = helper._binding(fixture)
            guessed_binding = helper._binding(fixture, cursor="cursor-2")
            origin = fixture[4]
            guessed_attempt = origin.prepare_direct(
                guessed_binding,
                recorded_at=NOW,
                account_acquisition_authority=fixture[5],
                account_acquisition=fixture[6],
            )
            root_response = helper._direct_response(
                fixture,
                root_binding,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":"cursor-2"}}',
                marker="root-after-early-prepare",
            )
            guessed_response = self._complete_prepared_response(
                fixture,
                guessed_binding,
                guessed_attempt,
                body=b'{"retCode":0,"result":{"list":[],"nextPageCursor":""}}',
                marker="guessed-observed-late",
            )
            origin_set = helper._origin_set(fixture, (root_response, guessed_response))
            with self.assertRaisesRegex(
                ProviderAccountPageChainError,
                "durable causal order",
            ):
                issue_provider_account_page_chain(
                    absence_semantics=fixture[7],
                    origin_set=origin_set,
                    observations=(
                        helper._observation(root_response, root_binding),
                        helper._observation(guessed_response, guessed_binding),
                    ),
                    qualification_registry=fixture[2],
                    surface=SURFACE,
                    at=NOW,
                )


if __name__ == "__main__":
    unittest.main()
