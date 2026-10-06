from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.dispatch as dispatch_module
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    SubmissionResponseBinding,
    load_submission_response_binding,
    require_canonical_submission_response_binding,
    submission_response_binding_projection,
)
from mvp.autotrade_mvp.persistence import JournalStore


class SubmissionResponseBindingAuthorityTests(unittest.TestCase):
    def _binding(self, path: str, *, ambiguous: bool = False):
        store = JournalStore(path)
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )
        response = ExactJsonTransportResponse(
            b'{"accepted":true}',
            http_status=200,
            requires_reconciliation=ambiguous,
            ambiguity_reason="provider_response_ambiguous" if ambiguous else None,
        )
        result = dispatcher.dispatch(
            attempt_id="authority-binding-a1",
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY"},
            now="2026-10-06T14:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=lambda _cid, _request, guard: (
                guard(),
                response,
            )[1],
            submission_scope={"endpoint": "/orders"},
        )
        self.assertEqual(result.status, "UNKNOWN" if ambiguous else "SENT")
        reopened = JournalStore(path)
        return load_submission_response_binding(
            reopened,
            environment="SIMULATION",
            account_id="acct",
            attempt_id="authority-binding-a1",
        )

    def test_registered_sent_binding_projects_exact_durable_state(self):
        with TemporaryDirectory() as directory:
            binding = self._binding(f"{directory}/journal.sqlite3")
            self.assertIs(
                require_canonical_submission_response_binding(binding),
                require_canonical_submission_response_binding(binding),
            )
            projected = submission_response_binding_projection(binding)
            self.assertEqual(projected["attempt_id"], "authority-binding-a1")
            self.assertEqual(projected["provider"], "provider")
            self.assertEqual(projected["terminal_state"], "SENT")
            self.assertEqual(projected["response_encoding"], "utf-8-json")
            self.assertIsNone(projected["ambiguity_reason"])
            self.assertIsNone(projected["retry_disposition"])
            self.assertEqual(projected["http_status"], 200)
            with self.assertRaises(TypeError):
                projected["provider"] = "forged"

    def test_restart_remints_binding_authority_from_durable_content(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = self._binding(path)
            second = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="authority-binding-a1",
            )
            self.assertIsNot(first, second)
            self.assertEqual(
                dict(submission_response_binding_projection(first)),
                dict(submission_response_binding_projection(second)),
            )

    def test_unregistered_exact_type_clone_cannot_mint_binding_authority(self):
        with TemporaryDirectory() as directory:
            binding = self._binding(f"{directory}/journal.sqlite3")
            clone = object.__new__(SubmissionResponseBinding)
            for name in (
                "attempt_id",
                "aggregate_id",
                "provider",
                "request_hash",
                "client_order_id",
                "environment",
                "account_id",
                "prepared_at",
                "sent_at",
                "submission_scope",
                "submission_scope_hash",
                "response_bytes",
                "response_sha256",
                "response_encoding",
                "terminal_state",
                "ambiguity_reason",
                "retry_disposition",
                "http_status",
                "_factory_token",
            ):
                object.__setattr__(
                    clone,
                    name,
                    object.__getattribute__(binding, name),
                )
            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                submission_response_binding_projection(clone)

    def test_post_mint_retarget_is_rejected_before_projection(self):
        with TemporaryDirectory() as directory:
            binding = self._binding(f"{directory}/journal.sqlite3")
            object.__setattr__(binding, "provider", "forged-provider")
            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                submission_response_binding_projection(binding)

    def test_module_token_rebinding_fails_closed(self):
        with TemporaryDirectory() as directory:
            binding = self._binding(f"{directory}/journal.sqlite3")
            original = dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN
            try:
                dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN = object()
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    submission_response_binding_projection(binding)
            finally:
                dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN = original
            self.assertEqual(
                submission_response_binding_projection(binding)["provider"],
                "provider",
            )

    def test_exact_ambiguous_response_remains_unknown_reconcile_first(self):
        with TemporaryDirectory() as directory:
            binding = self._binding(
                f"{directory}/journal.sqlite3",
                ambiguous=True,
            )
            projected = submission_response_binding_projection(binding)
            self.assertEqual(projected["terminal_state"], "UNKNOWN")
            self.assertEqual(
                projected["ambiguity_reason"],
                "provider_response_ambiguous",
            )
            self.assertEqual(
                projected["retry_disposition"],
                "RECONCILE_FIRST",
            )
            self.assertEqual(projected["response_encoding"], "utf-8-json")
            self.assertEqual(projected["http_status"], 200)


if __name__ == "__main__":
    unittest.main()
