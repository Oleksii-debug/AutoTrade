import json
import unittest
from pathlib import Path

from referencing import Registry, Resource
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
FIXTURES = ROOT / "contracts" / "fixtures"


class ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in SCHEMAS.glob("*.json")}
        cls.registry = Registry().with_resources(
            [(s["$id"], Resource.from_contents(s)) for s in cls.schemas.values()]
        )

    def validate(self, schema_name, def_name, fixture):
        schema = self.schemas[schema_name]
        Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/{def_name}"},
            registry=self.registry,
            format_checker=FormatChecker(),
        ).validate(fixture)

    def test_all_schemas_are_2020_12_valid(self):
        for name, schema in self.schemas.items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)

    def test_every_reference_resolves_in_the_local_schema_set(self):
        def refs(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    yield value["$ref"]
                for child in value.values():
                    yield from refs(child)
            elif isinstance(value, list):
                for child in value:
                    yield from refs(child)
        for name, schema in self.schemas.items():
            resolver = self.registry.resolver(schema["$id"])
            for ref in refs(schema):
                with self.subTest(schema=name, ref=ref):
                    self.assertIsInstance(resolver.lookup(ref).contents, dict)

    def test_model_request_fixture(self):
        self.validate("model.schema.json", "ModelRequest", json.loads((FIXTURES / "model-request.valid.json").read_text()))

    def test_order_intent_fixture(self):
        self.validate("execution.schema.json", "OrderIntent", json.loads((FIXTURES / "order-intent.valid.json").read_text()))

    def test_event_envelope_fixture(self):
        self.validate("event.schema.json", "EventEnvelope", json.loads((FIXTURES / "event-envelope.valid.json").read_text()))

    def test_dataset_manifest_fixture(self):
        self.validate("data.schema.json", "DatasetManifest", json.loads((FIXTURES / "dataset-manifest.valid.json").read_text()))

    def test_ui_command_fixture(self):
        self.validate("ui.schema.json", "UiCommand", json.loads((FIXTURES / "ui-command.valid.json").read_text()))

    def test_decimal_rejects_trailing_zero_exponent_and_negative_zero(self):
        common = self.schemas["common.schema.json"]
        validator = Draft202012Validator({"$ref": f"{common['$id']}#/$defs/Decimal"}, registry=self.registry)
        for bad in ["1.0", "1.20", "+1", "1e3", "-0"]:
            self.assertFalse(validator.is_valid(bad), bad)
        for good in ["0", "1", "1.2", "-1.25", "-0.5"]:
            self.assertTrue(validator.is_valid(good), good)

    def test_unknown_financial_command_fields_rejected(self):
        fixture = json.loads((FIXTURES / "order-intent.valid.json").read_text())
        fixture["surprise"] = "not allowed"
        schema = self.schemas["execution.schema.json"]
        validator = Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/OrderIntent"}, registry=self.registry, format_checker=FormatChecker())
        self.assertFalse(validator.is_valid(fixture))

    def test_provider_hard_cancellation_defaults_fail_closed(self):
        schema = self.schemas["model.schema.json"]
        capabilities = {"provider_id": "p", "kind": "CLOUD", "supports_private_data": True, "supports_tools": False, "supports_streaming": True, "supports_hard_cancellation": True}
        validator = Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/ProviderCapabilities"}, registry=self.registry)
        self.assertFalse(validator.is_valid(capabilities))

    def test_ui_command_unknown_fields_rejected(self):
        fixture = json.loads((FIXTURES / "ui-command.valid.json").read_text())
        fixture["financial_completion"] = True
        schema = self.schemas["ui.schema.json"]
        validator = Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/UiCommand"}, registry=self.registry)
        self.assertFalse(validator.is_valid(fixture))


if __name__ == "__main__":
    unittest.main()
