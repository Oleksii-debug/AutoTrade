import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.kraken_spot import (
    KrakenSpotOrderIntent,
    parse_spot_submission_response,
    prepare_spot_order_request,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = "2026-09-24T20:00:00Z"
NOW_DT = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
SOURCE = "https://api.kraken.com/0/private/AddOrder"


class KrakenSpotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in SCHEMAS.glob("*.json")
        }
        cls.registry = Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema)) for schema in cls.schemas.values()]
        )

    def validate_submission(self, value):
        schema = self.schemas["provider.schema.json"]
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/SubmissionResult"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(value)

    def prepared(self, client_order_id, *, environment="LIVE"):
        capability = CapabilitySnapshot(
            snapshot_id=str(uuid4()),
            provider_id="KRAKEN",
            account_id="spot-contract-account",
            entity_id="kraken-spot",
            environment=environment,
            instrument_version="XBTUSD:v1",
            observed_at=NOW_DT - timedelta(minutes=1),
            expires_at=NOW_DT + timedelta(minutes=1),
            supported_order_types=frozenset({"MARKET"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER_WRITE"}),
            position_mode="CASH",
            native_protection=frozenset(),
            rate_limit_policy_id="kraken-spot-contract",
            data_entitlements=frozenset({"ORDERS"}),
            evidence=(
                {
                    "artifact_id": str(uuid4()),
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": "2026-09-24T19:59:00Z",
                    "source_uri": "https://www.kraken.com/features/trading-api",
                },
            ),
            status="VERIFIED",
            sources=frozenset({"DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"}),
        )
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="XBTUSD",
            side="BUY",
            order_type="MARKET",
            volume="0.01",
        )
        return prepare_spot_order_request(
            intent,
            client_order_id=client_order_id,
            account_id="spot-contract-account",
            environment=environment,
            capability=capability,
            at=NOW_DT,
        )

    def test_ack_reject_and_transport_unknown_match_canonical_contract(self):
        acknowledged = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=self.prepared("spot-contract-1"),
            observed_at=NOW,
            source_uri=SOURCE,
            payload={"error": [], "result": {"txid": ["OABC-D123-E456"]}},
        )
        self.assertNotIn("provider_received_at", acknowledged)
        self.validate_submission(acknowledged)

        rejected = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=self.prepared("spot-contract-2"),
            observed_at=NOW,
            source_uri=SOURCE,
            payload={"error": ["EOrder:Insufficient funds"], "result": None},
        )
        self.assertNotIn("provider_received_at", rejected)
        self.validate_submission(rejected)

        unknown = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=self.prepared("spot-contract-3"),
            observed_at=NOW,
            source_uri=SOURCE,
            payload=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)

        with self.assertRaisesRegex(ValueError, "qualified only for LIVE"):
            parse_spot_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=self.prepared(
                    "spot-invalid-env",
                    environment="PAPER",
                ),
                observed_at=NOW,
                source_uri=SOURCE,
                payload=None,
                transport_ambiguous=True,
            )


if __name__ == "__main__":
    unittest.main()
