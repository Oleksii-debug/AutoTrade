import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.contract_version_guard import evaluate, evaluate_refs


def write_tree(root: Path, version="1.0.0", schemas=None, defs=None):
    schemas = ["a.schema.json"] if schemas is None else schemas
    defs = {"A": {"type": "string"}} if defs is None else defs
    base_uri = f"https://schemas.autotrade.local/{version}/"
    (root / "contracts" / "jsonschema").mkdir(parents=True)
    (root / "contracts" / "manifest.json").write_text(
        json.dumps({
            "contract_version": version,
            "schema_base_uri": base_uri,
            "schemas": schemas,
        }),
        encoding="utf-8",
    )
    for name in schemas:
        (root / "contracts" / "jsonschema" / name).write_text(
            json.dumps({"$id": base_uri + name, "$defs": defs}),
            encoding="utf-8",
        )


def write_openapi(
    root: Path,
    version: str,
    operations: list[tuple[str, str, str]],
    *,
    descriptions=None,
):
    descriptions = {} if descriptions is None else descriptions
    manifest_path = root / "contracts" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["openapi"] = {
        "path": "contracts/openapi/host-api.yaml",
        "version": version,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    path = root / "contracts" / "openapi" / "host-api.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "openapi: 3.1.0",
        "info:",
        "  title: test",
        f"  version: {version}",
        "paths:",
    ]
    for route, method, schema_ref in operations:
        lines.extend([
            f"  {route}:",
            f"    {method}:",
            f"      operationId: {method}_{route.replace('/', '_').replace('{', '').replace('}', '')}",
        ])
        description = descriptions.get((route, method))
        if description:
            lines.append(f"      description: {description}")
        lines.extend([
            "      responses:",
            '        "200":',
            "          content:",
            "            application/json:",
            "              schema:",
            f"                $ref: {schema_ref}",
        ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ContractVersionGuardTests(unittest.TestCase):
    def test_unchanged_surface_allows_same_version(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_tree(Path(right))
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_changed_surface_requires_version_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_tree(Path(right), defs={"A": {"type": "number"}})
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("without increasing" in item for item in errors))

    def test_added_required_member_requires_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            base_defs = {
                "A": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command_id"],
                    "properties": {
                        "command_id": {"type": "string"},
                        "account_id": {"type": "string"},
                    },
                }
            }
            current_defs = {
                "A": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command_id", "account_id"],
                    "properties": {
                        "command_id": {"type": "string"},
                        "account_id": {"type": "string"},
                    },
                }
            }
            write_tree(Path(left), defs=base_defs)
            write_tree(Path(right), version="1.1.0", defs=current_defs)
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("new required members" in item for item in errors))
            self.assertTrue(any("account_id" in item for item in errors))

    def test_first_required_member_on_existing_object_requires_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            base_defs = {
                "A": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "account_id": {"type": "string"},
                    },
                }
            }
            current_defs = {
                "A": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["account_id"],
                    "properties": {
                        "account_id": {"type": "string"},
                    },
                }
            }
            write_tree(Path(left), defs=base_defs)
            write_tree(Path(right), version="1.1.0", defs=current_defs)
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("new required members" in item for item in errors))
            self.assertTrue(any("account_id" in item for item in errors))

    def test_first_required_member_on_existing_object_allows_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            base_defs = {
                "A": {
                    "type": "object",
                    "properties": {
                        "environment": {"type": "string"},
                    },
                }
            }
            current_defs = {
                "A": {
                    "type": "object",
                    "required": ["environment"],
                    "properties": {
                        "environment": {"type": "string"},
                    },
                }
            }
            write_tree(Path(left), defs=base_defs)
            write_tree(Path(right), version="2.0.0", defs=current_defs)
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_added_required_member_allows_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            base_defs = {
                "A": {
                    "type": "object",
                    "required": ["command_id"],
                    "properties": {"command_id": {"type": "string"}},
                }
            }
            current_defs = {
                "A": {
                    "type": "object",
                    "required": ["command_id", "environment"],
                    "properties": {
                        "command_id": {"type": "string"},
                        "environment": {"type": "string"},
                    },
                }
            }
            write_tree(Path(left), defs=base_defs)
            write_tree(Path(right), version="2.0.0", defs=current_defs)
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_required_members_in_new_definition_do_not_force_major(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {"type": "string"}})
            write_tree(
                Path(right),
                version="1.1.0",
                defs={
                    "A": {"type": "string"},
                    "B": {
                        "type": "object",
                        "required": ["value"],
                        "properties": {"value": {"type": "string"}},
                    },
                },
            )
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_removed_definition_requires_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {}, "B": {}})
            write_tree(Path(right), version="1.1.0", defs={"A": {}})
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("new major version" in item for item in errors))

    def test_removed_definition_allows_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {}, "B": {}})
            write_tree(Path(right), version="2.0.0", defs={"A": {}})
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_existing_definition_change_requires_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {"type": "string"}})
            write_tree(Path(right), version="1.1.0", defs={"A": {"type": "number"}})
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("changed existing definitions" in item for item in errors))

    def test_existing_definition_change_allows_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {"type": "string"}})
            write_tree(Path(right), version="2.0.0", defs={"A": {"type": "number"}})
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_additive_definition_allows_minor_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), defs={"A": {"type": "string"}})
            write_tree(
                Path(right),
                version="1.1.0",
                defs={"A": {"type": "string"}, "B": {"type": "integer"}},
            )
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_version_only_local_ref_change_is_not_semantic_break(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(
                Path(left),
                defs={
                    "A": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "$ref": "https://schemas.autotrade.local/1.0.0/a.schema.json#/$defs/B"
                            }
                        },
                    },
                    "B": {"type": "string"},
                },
            )
            write_tree(
                Path(right),
                version="1.1.0",
                defs={
                    "A": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "$ref": "https://schemas.autotrade.local/1.1.0/a.schema.json#/$defs/B"
                            }
                        },
                    },
                    "B": {"type": "string"},
                },
            )
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_annotation_only_change_allows_patch_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(
                Path(left),
                defs={"A": {"type": "string", "description": "old wording"}},
            )
            write_tree(
                Path(right),
                version="1.0.1",
                defs={"A": {"type": "string", "description": "new wording"}},
            )
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_openapi_existing_operation_change_requires_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_openapi(
                Path(left),
                "1.0.0",
                [("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/A")],
            )
            write_tree(Path(right), version="1.1.0")
            write_openapi(
                Path(right),
                "1.1.0",
                [("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/B")],
            )
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("changed OpenAPI operations" in item for item in errors))

    def test_openapi_removed_operation_requires_major_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_openapi(
                Path(left),
                "1.0.0",
                [
                    ("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/A"),
                    ("/v1/health", "get", "../jsonschema/a.schema.json#/$defs/A"),
                ],
            )
            write_tree(Path(right), version="1.1.0")
            write_openapi(
                Path(right),
                "1.1.0",
                [("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/A")],
            )
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("removed OpenAPI operations" in item for item in errors))

    def test_openapi_additive_operation_allows_minor_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_openapi(
                Path(left),
                "1.0.0",
                [("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/A")],
            )
            write_tree(Path(right), version="1.1.0")
            write_openapi(
                Path(right),
                "1.1.0",
                [
                    ("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/A"),
                    ("/v1/health", "get", "../jsonschema/a.schema.json#/$defs/A"),
                ],
            )
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_openapi_description_only_change_allows_patch_increment(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left))
            write_openapi(
                Path(left),
                "1.0.0",
                [("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/A")],
                descriptions={("/v1/state", "get"): "old wording"},
            )
            write_tree(Path(right), version="1.0.1")
            write_openapi(
                Path(right),
                "1.0.1",
                [("/v1/state", "get", "../jsonschema/a.schema.json#/$defs/A")],
                descriptions={("/v1/state", "get"): "new wording"},
            )
            self.assertEqual(evaluate(Path(left), Path(right)), [])

    def test_ref_evaluation_uses_clean_archives_for_both_sides(self):
        with TemporaryDirectory() as base_dir, TemporaryDirectory() as current_dir:
            base = Path(base_dir)
            current = Path(current_dir)
            write_tree(base)
            write_tree(current)

            with patch(
                "tools.contract_version_guard.export_ref",
                side_effect=[base, current],
            ) as export:
                self.assertEqual(evaluate_refs("origin/main"), [])
                self.assertEqual(
                    [call.args[0] for call in export.call_args_list],
                    ["origin/main", "HEAD"],
                )

    def test_version_cannot_decrease(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            write_tree(Path(left), version="2.0.0")
            write_tree(Path(right), version="1.9.9")
            errors = evaluate(Path(left), Path(right))
            self.assertTrue(any("never decrease" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
