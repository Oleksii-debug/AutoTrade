from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.kraken_futures import (
    parse_submission_response,
    prepare_order_request,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW_DT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
NOW = "2026-09-24T20:00:00Z"


def capability(environment: str):
    observed = NOW_DT - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="KRAKEN",
            account_id="contract-account",
            entity_id="contract-entity",
            environment=environment,
            instrument_version="PI_XBTUSD@v1",
            observed_at=observed,
            expires_at=NOW_DT + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="contract",
            data_entitlements=frozenset(),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "3" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=NOW_DT,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def prepared(client_order_id: str, environment: str):
    return prepare_order_request(
        capability=capability(environment),
        account_id="contract-account",
        environment=environment,
        instrument_version="PI_XBTUSD@v1",
        at=NOW_DT,
        symbol="PI_XBTUSD",
        side="BUY",
        order_type="MARKET",
        size="1",
        client_order_id=client_order_id,
    )


def raw(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class KrakenFuturesContractTests(unittest.TestCase):
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

    def test_acknowledged_and_unknown_match_canonical_provider_contract(self):
        acknowledged = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("fut-contract-1", "DEMO"),
            observed_at=NOW,
            response_bytes=raw(
                {
                    "result": "success",
                    "sendStatus": {
                        "order_id": "provider-order-1",
                        "status": "placed",
                    },
                }
            ),
        )
        self.assertNotIn("provider_received_at", acknowledged)
        self.validate_submission(acknowledged)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("fut-contract-2", "LIVE"),
            observed_at=NOW,
            response_bytes=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)


if __name__ == "__main__":
    unittest.main()
