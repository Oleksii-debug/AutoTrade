from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import submission_attempt_aggregate_id
from mvp.autotrade_mvp.financial_request_binding import FinancialRequestBindingMaterial
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthorityError,
    _require_durable_prepared_financial_request,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


RS = "risk-snapshot:sha256:" + "1" * 64
RD = "risk:sha256:" + "2" * 64
AC = "provider-account-cut:sha256:" + "3" * 64
PS = "provider-financial-scope:sha256:" + "4" * 64
Q = "provider-qualification:sha256:" + "5" * 64
D6 = "sha256:" + "6" * 64
D7 = "sha256:" + "7" * 64
D8 = "sha256:" + "8" * 64


def exact_scope(prepared_request_sha256: str) -> dict[str, object]:
    return {
        "provider_id": "BYBIT",
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "capability_snapshot_id": "capability-1",
        "endpoint": "/v5/order/create",
        "prepared_request_sha256": prepared_request_sha256,
        "capability_snapshot_ids": ["capability-1"],
        "instrument_versions": ["7"],
        "provider_route_qualification_id": "qualification-1",
        "provider_route_capability_snapshot_id": "capability-1",
        "provider_route_decision_journal_sequence_cut": 41,
        "provider_route_provider_environment": "TESTNET",
        "provider_route_adapter_code_sha": "adapter-sha",
        "provider_route_packaged_artifact_digest": "sha256:" + "9" * 64,
        "provider_route_protocol_id": "protocol-1",
        "provider_route_protocol_version": "1",
        "provider_route_entity_policy_id": "linear-order-v1",
        "provider_route_entity_id": "entity-1",
    }


def exact_request() -> tuple[dict[str, object], str]:
    body = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Limit",
        "qty": "2",
        "timeInForce": "GTC",
        "orderLinkId": "client-order-1",
        "price": "30000",
        "reduceOnly": False,
        "positionIdx": 0,
    }
    body_sha = payload_digest(body)
    request = {
        "endpoint": "/v5/order/create",
        "body": body,
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "capability_snapshot_id": "capability-1",
        "entity_id": "entity-1",
        "capability_snapshot_ids": ["capability-1"],
        "instrument_versions": ["7"],
        "body_sha256": body_sha,
    }
    return request, body_sha


def binding() -> FinancialRequestBindingMaterial:
    request, body_sha = exact_request()
    scope = exact_scope(payload_digest(request))
    return FinancialRequestBindingMaterial(
        risk_snapshot_id=RS,
        risk_decision_id=RD,
        admitted_journal_sequence_cut=41,
        account_cut_id=AC,
        account_cut_digest=D6,
        account_head_journal_sequence=37,
        reservation_id="reservation-1",
        reservation_scope_digest=D7,
        reservation_version=3,
        reservation_state_digest=D8,
        provider_scope_digest=PS,
        provider_id="BYBIT",
        account_id="account-1",
        runtime_environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="linear-order-v1",
        instrument_id="00000000-0000-0000-0000-000000000101",
        instrument_version=7,
        quantity_unit="CONTRACT",
        equivalent_exposure_digest=D6,
        capability_snapshot_id="capability-1",
        qualification_identity_digest=Q,
        client_order_id="client-order-1",
        side="BUY",
        quantity="2",
        price="30000",
        price_semantics_digest=D7,
        order_type="LIMIT",
        time_in_force="GTC",
        reduce_only=False,
        trigger_protection_digest=D8,
        endpoint="/v5/order/create",
        query_sha256=payload_digest({}),
        body_sha256=body_sha,
        request_sha256=payload_digest(request),
        submission_scope_digest=payload_digest(scope),
    )


def prepared_event(
    material: FinancialRequestBindingMaterial,
    scope: dict[str, object],
    *,
    request_hash: str | None = None,
    intent_hash: str = "intent-hash-1",
) -> dict[str, object]:
    attempt_id = "attempt-1"
    aggregate_id = submission_attempt_aggregate_id(
        environment=material.runtime_environment,
        account_id=material.account_id,
        attempt_id=attempt_id,
    )
    return {
        "event_type": "SubmissionPrepared",
        "aggregate_id": aggregate_id,
        "aggregate_version": 1,
        "payload": {
            "attempt_id": attempt_id,
            "intent_id": "intent-1",
            "intent_hash": intent_hash,
            "provider": material.provider_id,
            "request_hash": material.request_sha256 if request_hash is None else request_hash,
            "client_order_id": material.client_order_id,
            "environment": material.runtime_environment,
            "account_id": material.account_id,
            "submission_scope": dict(scope),
            "submission_scope_hash": payload_digest(scope),
        },
    }


class DurablePreparedFinancialAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.journal = JournalStore(Path(self._temp.name) / "journal.db")
        self.material = binding()
        self.scope = exact_scope(self.material.request_sha256)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def check(self, events: list[dict[str, object]], *, checked_intent_hash: str = "intent-hash-1") -> None:
        expected_aggregate_id = submission_attempt_aggregate_id(
            environment=self.material.runtime_environment,
            account_id=self.material.account_id,
            attempt_id="attempt-1",
        )

        def load_events(store, aggregate_type, aggregate_id):
            self.assertIs(store, self.journal)
            self.assertEqual(aggregate_type, "submission_attempt")
            self.assertEqual(aggregate_id, expected_aggregate_id)
            return events

        _require_durable_prepared_financial_request(
            journal=self.journal,
            load_events=load_events,
            aggregate_id_function=submission_attempt_aggregate_id,
            binding=self.material,
            attempt_id="attempt-1",
            intent_id="intent-1",
            intent_hash="intent-hash-1",
            checked_intent_hash=checked_intent_hash,
            submission_scope=self.scope,
        )

    def test_exact_durable_prepared_event_is_accepted(self) -> None:
        self.check([prepared_event(self.material, self.scope)])

    def test_durable_request_substitution_is_rejected(self) -> None:
        event = prepared_event(
            self.material,
            self.scope,
            request_hash="sha256:" + "0" * 64,
        )
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "durable SubmissionPrepared scope differs",
        ):
            self.check([event])

    def test_durable_provider_scope_substitution_is_rejected(self) -> None:
        event = prepared_event(self.material, self.scope)
        event["payload"]["submission_scope"]["provider_environment"] = "DEMO"
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "provider scope differs",
        ):
            self.check([event])

    def test_checked_intent_substitution_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "dispatcher authority intent differs",
        ):
            self.check(
                [prepared_event(self.material, self.scope)],
                checked_intent_hash="other-intent-hash",
            )

    def test_guard_rejects_state_that_has_advanced_past_prepared(self) -> None:
        prepared = prepared_event(self.material, self.scope)
        sending = {
            "event_type": "SubmissionSending",
            "aggregate_id": prepared["aggregate_id"],
            "aggregate_version": 2,
            "payload": {"client_order_id": self.material.client_order_id},
        }
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "one durable SubmissionPrepared event",
        ):
            self.check([prepared, sending])


if __name__ == "__main__":
    unittest.main()
