import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.alpaca import parse_submission_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = "2026-09-24T20:00:00Z"


class AlpacaContractTests(unittest.TestCase):
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

    def test_acknowledged_and_transport_unknown_match_submission_contract(self):
        acknowledged = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="alpaca-contract-ack",
            response={
                "id": str(uuid4()),
                "client_order_id": "alpaca-contract-ack",
                "status": "accepted",
            },
            observed_at=NOW,
            environment="PAPER",
        )
        self.validate_submission(acknowledged)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="alpaca-contract-unknown",
            response=None,
            observed_at=NOW,
            environment="LIVE",
            transport_ambiguous=True,
        )
        self.validate_submission(unknown)
        self.assertNotIn("provider_received_at", unknown)
        self.assertEqual(unknown["outcome"], "UNKNOWN")
        self.assertEqual(unknown["retry_disposition"], "RECONCILE_FIRST")


if __name__ == "__main__":
    unittest.main()
