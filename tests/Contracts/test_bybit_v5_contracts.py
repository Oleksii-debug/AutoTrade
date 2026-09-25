import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.bybit_v5 import parse_submission_response


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"


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
            client_order_id="contract-ok",
            environment="MAINNET",
            response={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "bybit-order-1",
                    "orderLinkId": "contract-ok",
                },
                "retExtInfo": {},
                "time": 1790280000123,
            },
        )
        self.validate_submission(accepted)

        unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="contract-unknown",
            environment="MAINNET",
            response={
                "retCode": 10000,
                "retMsg": "Server Timeout",
                "result": {},
                "retExtInfo": {},
                "time": 1790280000123,
            },
        )
        self.validate_submission(unknown)

        transport_unknown = parse_submission_response(
            attempt_id=str(uuid4()),
            client_order_id="contract-transport-unknown",
            environment="MAINNET",
            response=None,
            observed_at="2026-09-24T20:00:00Z",
            transport_ambiguous=True,
        )
        self.assertNotIn("provider_received_at", transport_unknown)
        self.validate_submission(transport_unknown)


if __name__ == "__main__":
    unittest.main()
