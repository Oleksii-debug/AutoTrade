import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.kraken_spot import parse_spot_submission_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = "2026-09-24T20:00:00Z"
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

    def test_ack_reject_and_transport_unknown_match_canonical_contract(self):
        acknowledged = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="spot-contract-1",
            environment="LIVE",
            observed_at=NOW,
            source_uri=SOURCE,
            payload={"error": [], "result": {"txid": ["OABC-D123-E456"]}},
        )
        self.validate_submission(acknowledged)

        rejected = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="spot-contract-2",
            environment="LIVE",
            observed_at=NOW,
            source_uri=SOURCE,
            payload={"error": ["EOrder:Insufficient funds"], "result": None},
        )
        self.validate_submission(rejected)

        unknown = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="spot-contract-3",
            environment="LIVE",
            observed_at=NOW,
            source_uri=SOURCE,
            payload=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)

        with self.assertRaisesRegex(ValueError, "environment"):
            parse_spot_submission_response(
                attempt_id=str(uuid4()),
                client_order_id="spot-contract-invalid-env",
                environment="MARS",
                observed_at=NOW,
                source_uri=SOURCE,
                payload=None,
                transport_ambiguous=True,
            )


if __name__ == "__main__":
    unittest.main()
