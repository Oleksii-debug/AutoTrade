import json
import re
import unittest
from pathlib import Path

from referencing import Registry, Resource
from jsonschema import Draft202012Validator, FormatChecker

from mvp.autotrade_mvp.host_actions import SUPPORTED_HOST_ACTIONS

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
SCHEMAS = CONTRACTS / "jsonschema"
FIXTURES = CONTRACTS / "fixtures"
MANIFEST = CONTRACTS / "manifest.json"
FIXTURE_MANIFEST = FIXTURES / "manifest.json"


class ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract_manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.fixture_manifest = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
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

    def test_contract_manifest_covers_exact_schema_set(self):
        expected = set(self.contract_manifest["schemas"])
        observed = set(self.schemas)
        self.assertEqual(observed, expected)

    def test_schema_ids_and_draft_match_contract_version(self):
        version = self.contract_manifest["contract_version"]
        draft = self.contract_manifest["json_schema_draft"]
        base_uri = self.contract_manifest["schema_base_uri"]
        self.assertEqual(base_uri, f"https://schemas.autotrade.local/{version}/")
        ids = []
        for name in self.contract_manifest["schemas"]:
            schema = self.schemas[name]
            with self.subTest(schema=name):
                self.assertEqual(schema["$schema"], draft)
                self.assertEqual(schema["$id"], f"{base_uri}{name}")
                self.assertNotIn("latest", schema["$id"].lower())
                ids.append(schema["$id"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_language_anchor_and_openapi_version_match_manifest(self):
        version = self.contract_manifest["contract_version"]
        anchors = self.contract_manifest["language_anchors"]
        self.assertEqual(set(anchors), {"csharp", "python", "typescript"})
        for language, relative in anchors.items():
            with self.subTest(language=language):
                self.assertTrue((ROOT / relative).is_file())

        csharp_path = ROOT / anchors["csharp"]
        csharp = csharp_path.read_text(encoding="utf-8")
        match = re.search(r'\bVersion\s*=\s*"([^"]+)"', csharp)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), version)

        python_anchor = (ROOT / anchors["python"]).read_text(encoding="utf-8")
        self.assertIn(f'CONTRACT_VERSION = "{version}"', python_anchor)

        typescript_anchor = (ROOT / anchors["typescript"]).read_text(encoding="utf-8")
        self.assertIn(f'CONTRACT_VERSION: "{version}"', typescript_anchor)

        typescript_runtime = (
            ROOT / "contracts" / "bindings" / "typescript" / "commonScalars.js"
        ).read_text(encoding="utf-8")
        self.assertIn(f'CONTRACT_VERSION = "{version}"', typescript_runtime)

        openapi_path = ROOT / self.contract_manifest["openapi"]["path"]
        openapi = openapi_path.read_text(encoding="utf-8")
        self.assertRegex(openapi, r"(?m)^openapi:\s*3\.1\.0\s*$")
        info_version = re.search(r"(?m)^\s{2}version:\s*([^\s#]+)\s*$", openapi)
        self.assertIsNotNone(info_version)
        self.assertEqual(info_version.group(1), version)
        self.assertEqual(self.contract_manifest["openapi"]["version"], version)

    def test_openapi_json_schema_references_resolve(self):
        openapi_path = ROOT / self.contract_manifest["openapi"]["path"]
        text = openapi_path.read_text(encoding="utf-8")
        refs = re.findall(r"(?m)^\s*\$ref:\s*([^\s#]+)(#[^\s]+)?\s*$", text)
        self.assertTrue(refs)
        for relative_path, fragment in refs:
            with self.subTest(ref=f"{relative_path}{fragment}"):
                target = (openapi_path.parent / relative_path).resolve()
                self.assertTrue(target.is_relative_to(ROOT.resolve()))
                self.assertTrue(target.is_file())
                document = json.loads(target.read_text(encoding="utf-8"))
                if fragment:
                    self.assertTrue(fragment.startswith("#/$defs/"))
                    definition = fragment.removeprefix("#/$defs/")
                    self.assertIn(definition, document.get("$defs", {}))

    def test_fixture_manifest_is_complete_and_valid(self):
        entries = self.fixture_manifest["fixtures"]
        listed = [entry["path"] for entry in entries]
        self.assertEqual(len(listed), len(set(listed)))
        corpus_paths = {
            entry["path"]
            for entry in self.fixture_manifest.get("conformance_corpora", [])
        }
        observed = {
            p.name
            for p in FIXTURES.glob("*.json")
            if p.name != "manifest.json" and p.name not in corpus_paths
        }
        self.assertEqual(observed, set(listed))
        for corpus_path in corpus_paths:
            self.assertTrue((FIXTURES / corpus_path).is_file())
        self.assertEqual(self.fixture_manifest["corpus_version"], self.contract_manifest["contract_version"])

        for entry in entries:
            with self.subTest(fixture=entry["path"]):
                self.assertEqual(entry["expected"], "valid")
                self.assertIn(entry["schema"], self.schemas)
                fixture = json.loads((FIXTURES / entry["path"]).read_text(encoding="utf-8"))
                self.validate(entry["schema"], entry["definition"], fixture)

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

    def test_ui_command_action_set_matches_runtime_policy_and_rejects_unknown(self):
        fixture = json.loads((FIXTURES / "ui-command.valid.json").read_text())
        schema = self.schemas["ui.schema.json"]
        action_schema = schema["$defs"]["UiCommand"]["properties"]["action"]
        self.assertEqual(tuple(action_schema["enum"]), SUPPORTED_HOST_ACTIONS)
        validator = Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/UiCommand"},
            registry=self.registry,
        )
        for action in (
            "AUTHORITY.REVOKE",
            "FUTURE_PRIVILEGED_ACTION",
            "block_new_exposure",
        ):
            candidate = dict(fixture)
            candidate["action"] = action
            with self.subTest(action=action):
                self.assertFalse(validator.is_valid(candidate))

    def test_ui_command_requires_account_and_environment_scope(self):
        fixture = json.loads((FIXTURES / "ui-command.valid.json").read_text())
        schema = self.schemas["ui.schema.json"]
        validator = Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/UiCommand"},
            registry=self.registry,
        )
        legacy_unscoped = dict(fixture)
        legacy_unscoped.pop("account_id")
        legacy_unscoped.pop("environment")
        self.assertFalse(validator.is_valid(legacy_unscoped))

    def test_ui_command_rejects_noncanonical_environment(self):
        fixture = json.loads((FIXTURES / "ui-command.valid.json").read_text())
        fixture["environment"] = "PRODUCTION"
        schema = self.schemas["ui.schema.json"]
        validator = Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/UiCommand"},
            registry=self.registry,
        )
        self.assertFalse(validator.is_valid(fixture))


if __name__ == "__main__":
    unittest.main()
