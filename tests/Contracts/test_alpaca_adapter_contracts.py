from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.alpaca import (
    AlpacaOrderIntent,
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
            provider_id="ALPACA",
            account_id="contract-account",
            entity_id="contract-entity",
            environment="PAPER",
            instrument_version="AAPL:v1",
            observed_at=observed,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"DAY"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="contract",
            data_entitlements=frozenset(),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "1" * 64,
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
        AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        ),
        client_order_id=client_order_id,
        account_id="contract-account",
        environment="PAPER",
        capability=capability(),
        at=NOW,
    )


def raw(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class AlpacaAdapterContractTests(unittest.TestCase):
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

    def test_submission_matches_canonical_provider_contract(self):
        value = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("contract-1"),
            response_bytes=raw(
                {
                    "id": str(uuid4()),
                    "client_order_id": "contract-1",
                    "status": "accepted",
                }
            ),
            observed_at="2026-09-24T20:00:00Z",
        )
        self.assertNotIn("provider_received_at", value)
        self.validate_submission(value)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared("contract-unknown"),
            response_bytes=None,
            observed_at="2026-09-24T20:00:00Z",
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)


if __name__ == "__main__":
    unittest.main()
