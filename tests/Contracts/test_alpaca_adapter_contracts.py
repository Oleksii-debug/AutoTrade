import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.alpaca import parse_submission_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"


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

    def test_submission_matches_canonical_provider_contract(self):
        schema = self.schemas["provider.schema.json"]
        value = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="contract-1",
            response={
                "id": str(uuid4()),
                "client_order_id": "contract-1",
                "status": "accepted",
            },
            observed_at="2026-09-24T20:00:00Z",
            environment="PAPER",
        )
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/SubmissionResult"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(value)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="contract-unknown",
            response=None,
            observed_at="2026-09-24T20:00:00Z",
            environment="PAPER",
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", unknown)
        self.assertNotIn("observed_at", unknown)
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/SubmissionResult"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(unknown)


if __name__ == "__main__":
    unittest.main()
