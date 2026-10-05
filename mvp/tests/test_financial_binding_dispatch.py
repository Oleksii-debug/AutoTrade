from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher, stable_client_order_id
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingRegistry,
)
from mvp.autotrade_mvp.financial_binding_dispatch import (
    FinancialBindingDispatchError,
    dispatch_durable_financial_request,
    financial_submission_scope_digest,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.tests.test_confirmed_pending_admission import NOW
from mvp.tests.test_durable_financial_request_binding import (
    DurableFinancialRequestBindingTests,
)


class FinancialBindingDispatchTests(unittest.TestCase):
    @staticmethod
    def _fixture():
        return DurableFinancialRequestBindingTests(methodName="runTest")

    def _bound_case(self, store: JournalStore, *, scope_override=None, client_override=None):
        fixture = self._fixture()
        _source, admitted = fixture._admitted_case(store)
        material = fixture._material(store, admitted)
        request = {
            "endpoint": "/orders",
            "body": {
                "symbol": "ABC",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
            },
        }
        request_sha256 = "sha256:" + sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()
        client_order_id = stable_client_order_id(
            material.provider_id,
            admitted.intent_id,
            environment=material.runtime_environment,
            account_id=material.account_id,
        )
        if client_override is not None:
            client_order_id = client_override
        material = replace(
            material,
            request_sha256=request_sha256,
            client_order_id=client_order_id,
        )
        scope_digest = financial_submission_scope_digest(
            admission_id=admitted.admission_id,
            material=material,
        )
        material = replace(
            material,
            submission_scope_digest=(scope_override or scope_digest),
        )
        DurableFinancialRequestBindingRegistry(store).bind(
            admission_id=admitted.admission_id,
            material=material,
            bound_at=(NOW + timedelta(seconds=2)).isoformat().replace(
                "+00:00", "Z"
            ),
        )
        return admitted, material, request

    @staticmethod
    def _allow(_intent_hash, _now):
        return True, "allowed"

    @staticmethod
    def _send(_client_id, _request, final_guard):
        final_guard()
        return {"provider_order_id": "sim-order-1"}

    def test_exact_durable_request_enters_existing_guarded_chronology(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token="owner-1",
            )

            result = dispatch_durable_financial_request(
                dispatcher,
                admission_id=admitted.admission_id,
                attempt_id="financial-attempt-1",
                request=request,
                now=(NOW + timedelta(seconds=3)).isoformat().replace(
                    "+00:00", "Z"
                ),
                authority_check=self._allow,
                transport_send=self._send,
            )
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.client_order_id, material.client_order_id)

            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("financial-attempt-1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            prepared = events[0]["payload"]
            self.assertEqual(
                prepared["submission_scope_hash"],
                material.submission_scope_digest,
            )
            self.assertEqual(
                prepared["submission_scope"]["admission_id"],
                admitted.admission_id,
            )
            self.assertEqual(
                prepared["submission_scope"]["request_sha256"],
                material.request_sha256,
            )

    def test_request_drift_fails_before_submission_mutation_or_transport(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token="owner-1",
            )
            outbound = 0

            def transport(_client_id, _request, _final_guard):
                nonlocal outbound
                outbound += 1
                return {"must": "not happen"}

            changed = {
                **request,
                "body": {**request["body"], "quantity": "2"},
            }
            with self.assertRaisesRegex(
                FinancialBindingDispatchError,
                "request bytes differ",
            ):
                dispatch_durable_financial_request(
                    dispatcher,
                    admission_id=admitted.admission_id,
                    attempt_id="financial-attempt-drift",
                    request=changed,
                    now=(NOW + timedelta(seconds=3)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    authority_check=self._allow,
                    transport_send=transport,
                )
            self.assertEqual(outbound, 0)
            self.assertEqual(
                store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id("financial-attempt-drift"),
                ),
                [],
            )

    def test_caller_cannot_replace_bound_submission_scope(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(
                store,
                scope_override="sha256:" + "9" * 64,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token="owner-1",
            )
            with self.assertRaisesRegex(
                FinancialBindingDispatchError,
                "submission scope differs",
            ):
                dispatch_durable_financial_request(
                    dispatcher,
                    admission_id=admitted.admission_id,
                    attempt_id="financial-attempt-scope",
                    request=request,
                    now=(NOW + timedelta(seconds=3)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    authority_check=self._allow,
                    transport_send=self._send,
                )
            self.assertEqual(
                store.load_events(
                    "submission_attempt",
                    dispatcher._aggregate_id("financial-attempt-scope"),
                ),
                [],
            )

    def test_client_order_id_must_derive_from_durable_admitted_intent(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            fixture = self._fixture()
            _source, admitted = fixture._admitted_case(store)
            material = fixture._material(store, admitted)
            other_client_id = stable_client_order_id(
                material.provider_id,
                "another-intent",
                environment=material.runtime_environment,
                account_id=material.account_id,
            )
            # Rebuild on a fresh store so the helper may persist this otherwise
            # shape-valid but semantically foreign client identity.
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(
                store,
                client_override=other_client_id,
            )
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token="owner-1",
            )
            with self.assertRaisesRegex(
                FinancialBindingDispatchError,
                "client_order_id differs",
            ):
                dispatch_durable_financial_request(
                    dispatcher,
                    admission_id=admitted.admission_id,
                    attempt_id="financial-attempt-client",
                    request=request,
                    now=(NOW + timedelta(seconds=3)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    authority_check=self._allow,
                    transport_send=self._send,
                )

    def test_ambiguous_send_keeps_bound_prepared_scope_for_reconciliation(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token="owner-1",
            )

            def ambiguous(_client_id, _request, final_guard):
                final_guard()
                raise TimeoutError("response lost after possible send")

            result = dispatch_durable_financial_request(
                dispatcher,
                admission_id=admitted.admission_id,
                attempt_id="financial-attempt-unknown",
                request=request,
                now=(NOW + timedelta(seconds=3)).isoformat().replace(
                    "+00:00", "Z"
                ),
                authority_check=self._allow,
                transport_send=ambiguous,
            )
            self.assertEqual(result.status, "UNKNOWN")
            events = store.load_events(
                "submission_attempt",
                dispatcher._aggregate_id("financial-attempt-unknown"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            self.assertEqual(
                events[0]["payload"]["submission_scope_hash"],
                material.submission_scope_digest,
            )

    def test_exact_retry_reuses_same_binding_and_does_not_send_again(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            admitted, material, request = self._bound_case(store)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id=material.account_id,
                owner_token="owner-1",
            )
            outbound = 0

            def transport(_client_id, _request, final_guard):
                nonlocal outbound
                final_guard()
                outbound += 1
                return {"provider_order_id": "sim-order-1"}

            kwargs = dict(
                admission_id=admitted.admission_id,
                attempt_id="financial-attempt-retry",
                request=request,
                authority_check=self._allow,
                transport_send=transport,
            )
            first = dispatch_durable_financial_request(
                dispatcher,
                now=(NOW + timedelta(seconds=3)).isoformat().replace(
                    "+00:00", "Z"
                ),
                **kwargs,
            )
            sequence = store.current_journal_sequence()
            second = dispatch_durable_financial_request(
                dispatcher,
                now=(NOW + timedelta(seconds=4)).isoformat().replace(
                    "+00:00", "Z"
                ),
                **kwargs,
            )
            self.assertEqual(first.status, "SENT")
            self.assertEqual(second.status, "SENT")
            self.assertEqual(outbound, 1)
            self.assertEqual(store.current_journal_sequence(), sequence)

    def test_paper_live_remains_zero_wire_until_sealed_production_union_exists(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="paper-1",
                owner_token="owner-1",
            )
            outbound = 0

            def transport(_client_id, _request, _guard):
                nonlocal outbound
                outbound += 1
                return {"must": "not happen"}

            with self.assertRaisesRegex(
                FinancialBindingDispatchError,
                "sealed production authority",
            ):
                dispatch_durable_financial_request(
                    dispatcher,
                    admission_id="any-admission",
                    attempt_id="paper-zero-wire",
                    request={},
                    now=(NOW + timedelta(seconds=3)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    authority_check=self._allow,
                    transport_send=transport,
                )
            self.assertEqual(outbound, 0)
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_public_composer_does_not_accept_financial_scope_or_client_id_overrides(self) -> None:
        parameters = inspect.signature(
            dispatch_durable_financial_request
        ).parameters
        for forbidden in (
            "provider",
            "account_id",
            "environment",
            "intent_id",
            "intent_hash",
            "client_order_id",
            "client_id_format",
            "client_id_max_length",
            "submission_scope",
            "financial_request_binding_id",
        ):
            self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
