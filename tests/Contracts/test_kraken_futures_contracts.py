import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.kraken_futures import parse_submission_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = "2026-09-24T20:00:00Z"


class KrakenFuturesContractTests(unittest.TestCase):
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

    def test_acknowledged_and_unknown_match_canonical_provider_contract(self):
        acknowledged = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="fut-contract-1",
            environment="DEMO",
            observed_at=NOW,
            response={
                "result": "success",
                "sendStatus": {"order_id": "provider-order-1", "status": "placed"},
            },
        )
        self.validate_submission(acknowledged)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="fut-contract-2",
            environment="LIVE",
            observed_at=NOW,
            response=None,
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        self.validate_submission(unknown)


if __name__ == "__main__":
    unittest.main()
