import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.kraken import (
    parse_futures_submission_response,
    parse_spot_submission_response,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
NOW = "2026-09-24T20:00:00Z"


class KrakenContractTests(unittest.TestCase):
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

    def test_spot_acknowledgement_and_unknown_match_provider_contract(self):
        accepted = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="spot-contract-1",
            observed_at=NOW,
            response={
                "error": [],
                "result": {"txid": ["OABC12-DEF345-GHI678"]},
            },
        )
        self.validate_submission(accepted)

        unknown = parse_spot_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="spot-contract-2",
            observed_at=NOW,
            response=None,
            transport_ambiguous=True,
        )
        self.validate_submission(unknown)

    def test_futures_acknowledgement_and_unknown_match_provider_contract(self):
        accepted = parse_futures_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="fut-contract-1",
            environment="DEMO",
            observed_at=NOW,
            response={
                "result": "success",
                "sendStatus": {
                    "order_id": "futures-provider-order-1",
                    "status": "placed",
                },
            },
        )
        self.validate_submission(accepted)

        unknown = parse_futures_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="fut-contract-2",
            environment="LIVE",
            observed_at=NOW,
            response=None,
            transport_ambiguous=True,
        )
        self.validate_submission(unknown)


if __name__ == "__main__":
    unittest.main()
