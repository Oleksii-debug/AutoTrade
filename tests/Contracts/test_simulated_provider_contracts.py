import json
from pathlib import Path
import unittest
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from mvp.autotrade_mvp.simulated_provider import SimulatedProvider


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"


class SimulatedProviderContractTests(unittest.TestCase):
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

    def validate(self, schema_name, def_name, value):
        schema = self.schemas[schema_name]
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/{def_name}"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(value)

    def test_simulated_outputs_match_canonical_provider_and_execution_contracts(self):
        provider = SimulatedProvider(initial_cash="1000")
        attempt_id = str(uuid4())
        submission = provider.submit_order(
            attempt_id=attempt_id,
            client_order_id="client-1",
            instrument_version="ABC@version-1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        self.validate(
            "provider.schema.json",
            "SubmissionResult",
            submission,
        )
        fills = provider.activity_fills()
        self.assertEqual(len(fills), 1)
        self.validate(
            "execution.schema.json",
            "ExecutionFill",
            fills[0],
        )
        snapshot = provider.account_snapshot(
            now="2026-09-24T18:01:00Z"
        )
        self.validate(
            "provider.schema.json",
            "AccountSnapshot",
            snapshot,
        )
        query = provider.query_order(
            client_order_id="client-1",
            coverage_start="2026-09-24T17:00:00Z",
            coverage_end="2026-09-24T19:00:00Z",
            pagination_complete=True,
            now="2026-09-24T19:00:00Z",
        )
        self.validate(
            "provider.schema.json",
            "QueryOrderResult",
            query,
        )
        health = provider.health(now="2026-09-24T19:00:00Z")
        self.validate(
            "provider.schema.json",
            "HealthResult",
            health,
        )

    def test_short_net_position_preserves_negative_quantity_in_contract(self):
        provider = SimulatedProvider(initial_cash="1000")
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="short-1",
            instrument_version="ABC@version-1",
            side="SELL",
            quantity="2",
            price="100",
            now="2026-09-24T18:00:00Z",
        )
        snapshot = provider.account_snapshot(
            now="2026-09-24T18:01:00Z"
        )
        self.validate(
            "provider.schema.json",
            "AccountSnapshot",
            snapshot,
        )
        self.assertEqual(
            snapshot["positions"][0]["quantity"]["value"],
            "-2",
        )


if __name__ == "__main__":
    unittest.main()
