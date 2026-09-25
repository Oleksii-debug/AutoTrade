from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.bybit_v5 import (
    parse_submission_response,
    prepare_order_request,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability():
    observed = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="contract-account",
            entity_id="contract-entity",
            environment="MAINNET",
            instrument_version="BTCUSDT@v1",
            observed_at=observed,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"IOC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="contract",
            data_entitlements=frozenset(),
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


def prepared(client_order_id: str):
    return prepare_order_request(
        capability=capability(),
        account_id="contract-account",
        environment="MAINNET",
        instrument_version="BTCUSDT@v1",
        at=NOW,
        product_family="SPOT",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity="1",
        client_order_id=client_order_id,
        time_in_force="IOC",
    )


def raw(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


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
        accepted = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("contract-ok"),
            response_bytes=raw(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "bybit-order-1",
                        "orderLinkId": "contract-ok",
                    },
                    "retExtInfo": {},
                    "time": 1790280000123,
                }
            ),
            observed_at="2026-09-24T21:00:00Z",
        )
        self.assertEqual(
            accepted["provider_received_at"],
            "2026-09-24T20:00:00.123Z",
        )
        self.assertEqual(
            accepted["evidence"][0]["observed_at"],
            "2026-09-24T21:00:00Z",
        )
        self.validate_submission(accepted)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("contract-unknown"),
            response_bytes=raw(
                {
                    "retCode": 10000,
                    "retMsg": "Server Timeout",
                    "result": {},
                    "retExtInfo": {},
                    "time": 1790280000123,
                }
            ),
            observed_at="2026-09-24T21:00:01Z",
        )
        self.assertEqual(
            unknown["provider_received_at"],
            "2026-09-24T20:00:00.123Z",
        )
        self.assertEqual(
            unknown["evidence"][0]["observed_at"],
            "2026-09-24T21:00:01Z",
        )
        self.validate_submission(unknown)

        transport_unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("contract-transport-unknown"),
            response_bytes=None,
            observed_at="2026-09-24T20:00:00Z",
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", transport_unknown)
        self.validate_submission(transport_unknown)


if __name__ == "__main__":
    unittest.main()
