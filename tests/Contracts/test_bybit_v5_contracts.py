from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.bybit_v5 import (
    parse_submission_response,
    prepare_order_submission,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import observe_submission_json_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def write_capability():
    observed = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="contract-account",
            entity_id="contract-order",
            environment="LIVE",
            instrument_version="BTCUSDT@v1",
            observed_at=observed,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"IOC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="contract",
            data_entitlements=frozenset({"ORDERS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "2" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def durable_submission(payload, *, intent_id):
    attempt_id = str(uuid4())
    client_order_id = stable_client_order_id(
        "BYBIT",
        intent_id,
        environment="LIVE",
        account_id="contract-account",
    )
    prepared = prepare_order_submission(
        capability=write_capability(),
        at=NOW,
        provider_environment="MAINNET",
        product_family="SPOT",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="0.01",
        client_order_id=client_order_id,
        time_in_force="IOC",
    )
    material = json.loads(json.dumps(payload))
    result = material.get("result")
    if isinstance(result, dict) and result.get("orderLinkId") == "__CLIENT__":
        result["orderLinkId"] = client_order_id
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    with TemporaryDirectory() as directory:
        store = JournalStore(f"{directory}/journal.sqlite3")
        dispatcher = GuardedDispatcher(
            store,
            environment="LIVE",
            account_id="contract-account",
            owner_token="contract-owner",
        )
        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash="contract-intent-hash",
            provider="BYBIT",
            request=prepared.body,
            now="2026-09-24T20:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=lambda _cid, _request, guard: (
                guard(),
                ExactJsonTransportResponse(raw),
            )[1],
            sender_check=lambda _owner, _epoch: None,
            submission_scope={
                "endpoint": prepared.endpoint,
                "prepared_request_sha256": prepared.body_sha256,
                "capability_snapshot_ids": list(prepared.capability_snapshot_ids),
                "instrument_versions": list(prepared.instrument_versions),
            },
        )
        if outcome.status != "SENT":
            raise AssertionError(f"guarded dispatch did not persist SENT: {outcome}")
        binding = load_submission_response_binding(
            store,
            environment="LIVE",
            account_id="contract-account",
            attempt_id=attempt_id,
        )
        observation = observe_submission_json_response(
            response_binding=binding,
            provider_id="BYBIT",
            endpoint=prepared.endpoint,
            prepared_request_sha256=prepared.body_sha256,
            capability_snapshot_ids=prepared.capability_snapshot_ids,
            instrument_versions=prepared.instrument_versions,
        )
    return attempt_id, prepared, observation


class BybitV5ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in SCHEMAS.glob("*.json")
        }
        cls.registry = Registry().with_resources(
            [
                (schema["$id"], Resource.from_contents(schema))
                for schema in cls.schemas.values()
            ]
        )

    def validate_submission(self, value):
        schema = self.schemas["provider.schema.json"]
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/SubmissionResult"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(value)

    def test_success_and_ambiguous_results_match_provider_contract(self):
        attempt, prepared, observation = durable_submission(
            {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "bybit-order-1",
                    "orderLinkId": "__CLIENT__",
                },
                "retExtInfo": {},
                "time": 1790280000123,
            },
            intent_id="contract-bybit-ok",
        )
        accepted = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared,
            observation=observation,
        )
        self.assertEqual(
            accepted["provider_received_at"],
            "2026-09-24T20:00:00.123Z",
        )
        self.assertEqual(
            accepted["evidence"][0]["sha256"],
            observation.response_sha256,
        )
        self.validate_submission(accepted)

        attempt, prepared, observation = durable_submission(
            {
                "retCode": 10000,
                "retMsg": "Server Timeout",
                "result": {},
                "retExtInfo": {},
                "time": 1790280000123,
            },
            intent_id="contract-bybit-unknown",
        )
        unknown = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared,
            observation=observation,
        )
        self.assertEqual(unknown["outcome"], "UNKNOWN")
        self.assertEqual(unknown["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(
            unknown["evidence"][0]["sha256"],
            observation.response_sha256,
        )
        self.validate_submission(unknown)

        client_order_id = stable_client_order_id(
            "BYBIT",
            "contract-bybit-transport-unknown",
            environment="LIVE",
            account_id="contract-account",
        )
        prepared = prepare_order_submission(
            capability=write_capability(),
            at=NOW,
            provider_environment="MAINNET",
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity="0.01",
            client_order_id=client_order_id,
            time_in_force="IOC",
        )
        transport_unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared,
            observation=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", transport_unknown)
        self.assertEqual(transport_unknown["evidence"], [])
        self.validate_submission(transport_unknown)


if __name__ == "__main__":
    unittest.main()
