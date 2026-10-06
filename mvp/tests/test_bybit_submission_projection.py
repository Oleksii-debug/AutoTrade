from __future__ import annotations

import json
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from autotrade_runtime.artifacts import ArtifactStore

import mvp.autotrade_mvp.bybit_submission_projection as bybit_submission_projection_module

from mvp.autotrade_mvp.bybit_submission_projection import (
    project_authenticated_bybit_submission,
)
from mvp.autotrade_mvp.bybit_v5 import (
    guarded_order_projection,
    guarded_order_request_sha256,
    prepare_order_submission,
)
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    stable_client_order_id,
)
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.tests.test_bybit_v5 import READ_AT, write_capability


class AuthenticatedBybitSubmissionProjectionTests(unittest.TestCase):
    def _sent(
        self,
        directory: str,
        response: dict[str, object],
        *,
        intent_id: str,
        attempt_id: str | None = None,
        quantity: str = "0.001",
    ):
        account_id = "bybit-account"
        environment = "PAPER"
        attempt = attempt_id or str(uuid4())
        store = JournalStore(f"{directory}/journal.sqlite3")
        artifacts = ArtifactStore(f"{directory}/artifacts")
        capability = write_capability(
            family="LINEAR_DERIVATIVES",
            position_mode="HEDGE",
            account_id=account_id,
            environment=environment,
            instrument_version="BTCUSDT@1",
            permission_scope="BYBIT.LINEAR.ORDER.WRITE",
            additional_permission_scopes=("ORDER_WRITE",),
            provider_environment="TESTNET",
        )
        client_order_id = stable_client_order_id(
            "BYBIT",
            intent_id,
            environment=environment,
            account_id=account_id,
        )
        prepared = prepare_order_submission(
            capability=capability,
            at=READ_AT,
            provider_environment="TESTNET",
            product_family="LINEAR_DERIVATIVES",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=quantity,
            client_order_id=client_order_id,
            time_in_force="GTC",
            price="50000",
            reduce_only=False,
            position_side="LONG",
            position_idx=1,
        )
        book = DurableOrderBookProjection(
            store,
            provider_id="BYBIT",
            account_id=account_id,
            environment=environment,
            host_id="host-1",
            owner_epoch="1",
            evidence_artifact_store=artifacts,
        )
        book.create_order(
            event_key="create:" + attempt,
            client_order_id=client_order_id,
            instrument=prepared.instrument_version,
            side="BUY",
            requested_quantity=quantity,
            committed_at=READ_AT.isoformat().replace("+00:00", "Z"),
        )

        payload = json.loads(json.dumps(response))
        result = payload.get("result")
        if isinstance(result, dict) and result.get("orderLinkId") == "__CLIENT__":
            result["orderLinkId"] = client_order_id
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        dispatcher = GuardedDispatcher(
            store,
            environment=environment,
            account_id=account_id,
            owner_token="owner",
        )
        request_sha256 = guarded_order_request_sha256(prepared)
        now = READ_AT.isoformat().replace("+00:00", "Z")
        outcome = dispatcher.dispatch(
            attempt_id=attempt,
            intent_id=intent_id,
            intent_hash="sha256:" + "4" * 64,
            provider="BYBIT",
            request=dict(guarded_order_projection(prepared)),
            now=now,
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=lambda _cid, _request, final_guard: (
                final_guard(),
                ExactJsonTransportResponse(raw, http_status=200),
            )[1],
            submission_scope={
                "endpoint": prepared.endpoint,
                "prepared_request_sha256": request_sha256,
                "capability_snapshot_ids": list(prepared.capability_snapshot_ids),
                "instrument_versions": list(prepared.instrument_versions),
                "provider_environment": prepared.provider_environment,
            },
        )
        self.assertEqual(outcome.status, "SENT")
        return store, artifacts, book, prepared, attempt, client_order_id

    def test_authenticated_ack_moves_generic_unknown_to_working_with_exact_evidence(self):
        with TemporaryDirectory() as directory:
            store, artifacts, book, prepared, attempt, client_order_id = self._sent(
                directory,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-ack-1",
                        "orderLinkId": "__CLIENT__",
                    },
                },
                intent_id="authenticated-ack",
            )

            result = project_authenticated_bybit_submission(
                book,
                attempt_id=attempt,
                prepared_request=prepared,
            )
            self.assertTrue(result.inserted)
            self.assertEqual(result.snapshot.state, "WORKING")
            self.assertEqual(result.snapshot.provider_order_id, "provider-ack-1")
            self.assertEqual(result.snapshot.filled_quantity, 0)

            events = store.load_events("order_projection_book", book.aggregate_id)
            self.assertEqual(
                [event["payload"]["operation"] for event in events],
                ["CREATE", "MARK_SEND_STARTED", "ACKNOWLEDGE", "ACKNOWLEDGE"],
            )
            generic_unknown = events[-2]
            self.assertEqual(
                generic_unknown["payload"]["request"]["status"],
                "UNKNOWN",
            )
            acknowledged = events[-1]
            self.assertEqual(
                acknowledged["payload"]["request"]["status"],
                "ACKNOWLEDGED",
            )
            self.assertEqual(len(acknowledged["evidence_refs"]), 1)
            evidence_ref = acknowledged["evidence_refs"][0]
            manifest = artifacts.load_manifest(evidence_ref["artifact_id"])
            self.assertEqual(manifest["sha256"], evidence_ref["sha256"])
            self.assertEqual(
                manifest["metadata"]["request_hash"],
                payload_digest(acknowledged["payload"]["request"]),
            )
            self.assertEqual(
                manifest["metadata"]["order_operation"],
                "ACKNOWLEDGE",
            )

            retry = project_authenticated_bybit_submission(
                book,
                attempt_id=attempt,
                prepared_request=prepared,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(retry.event_id, result.event_id)
            self.assertEqual(
                len(store.load_events("order_projection_book", book.aggregate_id)),
                4,
            )

    def test_authenticated_definitive_rejection_is_durable_and_evidenced(self):
        with TemporaryDirectory() as directory:
            store, _artifacts, book, prepared, attempt, _client_order_id = self._sent(
                directory,
                {
                    "retCode": 10001,
                    "retMsg": "request parameter error",
                    "result": {},
                },
                intent_id="authenticated-reject",
            )
            result = project_authenticated_bybit_submission(
                book,
                attempt_id=attempt,
                prepared_request=prepared,
            )
            self.assertEqual(result.snapshot.state, "REJECTED")
            self.assertIsNone(result.snapshot.provider_order_id)
            events = store.load_events("order_projection_book", book.aggregate_id)
            self.assertEqual(events[-1]["payload"]["request"]["status"], "REJECTED")
            self.assertEqual(len(events[-1]["evidence_refs"]), 1)

    def test_provider_specific_unknown_does_not_add_second_unknown_mutation(self):
        with TemporaryDirectory() as directory:
            store, _artifacts, book, prepared, attempt, _client_order_id = self._sent(
                directory,
                {
                    "retCode": 10016,
                    "retMsg": "server error",
                    "result": {},
                },
                intent_id="authenticated-still-unknown",
            )
            result = project_authenticated_bybit_submission(
                book,
                attempt_id=attempt,
                prepared_request=prepared,
            )
            self.assertEqual(result.snapshot.state, "UNKNOWN")
            self.assertIsNone(result.snapshot.provider_order_id)
            events = store.load_events("order_projection_book", book.aggregate_id)
            self.assertEqual(
                [event["payload"]["operation"] for event in events],
                ["CREATE", "MARK_SEND_STARTED", "ACKNOWLEDGE"],
            )
            self.assertEqual(events[-1]["payload"]["request"]["status"], "UNKNOWN")
            self.assertEqual(events[-1]["evidence_refs"], [])

    def test_wrong_prepared_request_fails_after_retaining_generic_unknown(self):
        with TemporaryDirectory() as directory:
            _store, _artifacts, book, prepared, attempt, client_order_id = self._sent(
                directory,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-wrong-prepared",
                        "orderLinkId": "__CLIENT__",
                    },
                },
                intent_id="authenticated-wrong-prepared",
            )
            capability = write_capability(
                family="LINEAR_DERIVATIVES",
                position_mode="HEDGE",
                account_id="bybit-account",
                environment="PAPER",
                instrument_version="BTCUSDT@1",
                permission_scope="BYBIT.LINEAR.ORDER.WRITE",
                additional_permission_scopes=("ORDER_WRITE",),
                provider_environment="TESTNET",
            )
            wrong = prepare_order_submission(
                capability=capability,
                at=READ_AT,
                provider_environment="TESTNET",
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.002",
                client_order_id=client_order_id,
                time_in_force="GTC",
                price="50000",
                reduce_only=False,
                position_side="LONG",
                position_idx=1,
            )
            with self.assertRaisesRegex(
                ProviderCoreError,
                "durable submission request digest mismatch",
            ):
                project_authenticated_bybit_submission(
                    book,
                    attempt_id=attempt,
                    prepared_request=wrong,
                )
            snapshot = book.order(client_order_id).snapshot()
            self.assertEqual(snapshot.state, "UNKNOWN")
            self.assertIsNone(snapshot.provider_order_id)


    def test_late_parser_global_rebinding_cannot_forge_ack(self):
        with TemporaryDirectory() as directory:
            _store, _artifacts, book, prepared, attempt, _client_order_id = self._sent(
                directory,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-authentic-parser",
                        "orderLinkId": "__CLIENT__",
                    },
                },
                intent_id="late-parser-rebinding",
            )
            calls = 0

            def hostile_parser(**_kwargs):
                nonlocal calls
                calls += 1
                return {
                    "outcome": "ACKNOWLEDGED",
                    "client_order_id": "forged-client",
                    "provider_order_id": "forged-provider-order",
                    "evidence": [],
                }

            original = bybit_submission_projection_module.parse_submission_response
            bybit_submission_projection_module.parse_submission_response = hostile_parser
            try:
                result = project_authenticated_bybit_submission(
                    book,
                    attempt_id=attempt,
                    prepared_request=prepared,
                )
            finally:
                bybit_submission_projection_module.parse_submission_response = original

            self.assertEqual(calls, 0)
            self.assertEqual(result.snapshot.state, "WORKING")
            self.assertEqual(
                result.snapshot.provider_order_id,
                "provider-authentic-parser",
            )

    def test_late_oms_acknowledge_rebinding_cannot_retarget_commit(self):
        with TemporaryDirectory() as directory:
            _store, _artifacts, book, prepared, attempt, _client_order_id = self._sent(
                directory,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-authentic-oms",
                        "orderLinkId": "__CLIENT__",
                    },
                },
                intent_id="late-oms-method-rebinding",
            )
            calls = 0
            original = DurableOrderBookProjection.acknowledge

            def hostile_acknowledge(_self, **_kwargs):
                nonlocal calls
                calls += 1
                raise AssertionError("hostile acknowledge must not execute")

            DurableOrderBookProjection.acknowledge = hostile_acknowledge
            try:
                result = project_authenticated_bybit_submission(
                    book,
                    attempt_id=attempt,
                    prepared_request=prepared,
                )
            finally:
                DurableOrderBookProjection.acknowledge = original

            self.assertEqual(calls, 0)
            self.assertEqual(result.snapshot.state, "WORKING")
            self.assertEqual(result.snapshot.provider_order_id, "provider-authentic-oms")

    def test_late_artifact_publisher_rebinding_cannot_retarget_evidence_write(self):
        with TemporaryDirectory() as directory:
            _store, artifacts, book, prepared, attempt, _client_order_id = self._sent(
                directory,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-authentic-artifact",
                        "orderLinkId": "__CLIENT__",
                    },
                },
                intent_id="late-artifact-publisher-rebinding",
            )
            calls = 0
            original = ArtifactStore.publish_bytes

            def hostile_publish(_self, **_kwargs):
                nonlocal calls
                calls += 1
                raise AssertionError("hostile artifact publisher must not execute")

            ArtifactStore.publish_bytes = hostile_publish
            try:
                result = project_authenticated_bybit_submission(
                    book,
                    attempt_id=attempt,
                    prepared_request=prepared,
                )
            finally:
                ArtifactStore.publish_bytes = original

            self.assertEqual(calls, 0)
            self.assertEqual(result.snapshot.state, "WORKING")
            events = _store.load_events("order_projection_book", book.aggregate_id)
            ref = events[-1]["evidence_refs"][0]
            self.assertEqual(
                artifacts.load_manifest(ref["artifact_id"])["sha256"],
                ref["sha256"],
            )

    def test_instance_method_shadow_fails_before_hostile_dispatch(self):
        with TemporaryDirectory() as directory:
            _store, _artifacts, book, prepared, attempt, _client_order_id = self._sent(
                directory,
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-shadowed",
                        "orderLinkId": "__CLIENT__",
                    },
                },
                intent_id="instance-shadow-before-authority",
            )
            calls = 0

            def hostile_order(_client_order_id):
                nonlocal calls
                calls += 1
                raise AssertionError("shadowed order method must not execute")

            book.order = hostile_order
            with self.assertRaisesRegex(
                Exception,
                "durable OMS instance state is shadowed",
            ):
                project_authenticated_bybit_submission(
                    book,
                    attempt_id=attempt,
                    prepared_request=prepared,
                )
            self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
