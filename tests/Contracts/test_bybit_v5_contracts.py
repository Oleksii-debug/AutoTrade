from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.bybit_v5 import (
    _classify_submission_response_payload,
    parse_submission_response,
    prepare_order_submission,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.dispatch import stable_client_order_id


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
            provider_environment="MAINNET",
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


def submission_fixture(payload, *, intent_id):
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
    return attempt_id, prepared, material


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
        attempt, prepared, payload = submission_fixture(
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
        accepted = {
            "attempt_id": attempt,
            **_classify_submission_response_payload(
                prepared_request=prepared,
                payload=payload,
            ),
            "evidence": [],
        }
        self.assertEqual(
            accepted["provider_received_at"],
            "2026-09-24T20:00:00.123Z",
        )
        self.assertEqual(accepted["evidence"], [])
        self.validate_submission(accepted)

        attempt, prepared, payload = submission_fixture(
            {
                "retCode": 10000,
                "retMsg": "Server Timeout",
                "result": {},
                "retExtInfo": {},
                "time": 1790280000123,
            },
            intent_id="contract-bybit-unknown",
        )
        unknown = {
            "attempt_id": attempt,
            **_classify_submission_response_payload(
                prepared_request=prepared,
                payload=payload,
            ),
            "evidence": [],
        }
        self.assertEqual(unknown["outcome"], "UNKNOWN")
        self.assertEqual(unknown["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(unknown["evidence"], [])
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
