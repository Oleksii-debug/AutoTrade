"""Generate cross-language closed-object shape conformance from canonical schemas.

This is deliberately a narrow generated binding, not a second JSON Schema
implementation. It covers the WP-01 cross-language dimensions that must not
drift across runtimes: additionalProperties=false, required-member presence,
direct string enums, and contract-version identity. Full structural validation
remains owned by the canonical Draft 2020-12 JSON Schemas.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "contracts" / "manifest.json"
SCHEMAS = ROOT / "contracts" / "jsonschema"
OUTPUTS = {
    "python": ROOT / "contracts" / "bindings" / "python" / "contract_shapes.py",
    "typescript_decl": ROOT / "contracts" / "bindings" / "typescript" / "contractShapes.d.ts",
    "typescript_runtime": ROOT / "contracts" / "bindings" / "typescript" / "contractShapes.js",
    "csharp": ROOT / "src" / "AutoTrade.Contracts" / "ContractShapeContracts.cs",
    "corpus": ROOT / "contracts" / "fixtures" / "contract-shapes.corpus.json",
}
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def load_shapes() -> tuple[str, dict[str, dict[str, object]]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = manifest.get("contract_version")
    base_uri = manifest.get("schema_base_uri")
    schema_names = manifest.get("schemas")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ValueError("manifest contract_version must be semantic version text")
    if not isinstance(base_uri, str) or not base_uri:
        raise ValueError("manifest schema_base_uri must be non-empty text")
    if (
        not isinstance(schema_names, list)
        or not schema_names
        or any(not isinstance(name, str) or not name for name in schema_names)
        or len(schema_names) != len(set(schema_names))
    ):
        raise ValueError("manifest schemas must be a unique non-empty string array")

    expected_shape_conformance = {
        "scope": "closed-object-shape-subset",
        "corpus": "contracts/fixtures/contract-shapes.corpus.json",
        "bindings": {
            "python": "contracts/bindings/python/contract_shapes.py",
            "csharp": "src/AutoTrade.Contracts/ContractShapeContracts.cs",
            "typescript": "contracts/bindings/typescript/contractShapes.js",
        },
    }
    if manifest.get("shape_conformance") != expected_shape_conformance:
        raise ValueError(
            "manifest shape_conformance must name the generated corpus and "
            "python/csharp/typescript bindings"
        )

    shapes: dict[str, dict[str, object]] = {}
    for schema_name in schema_names:
        path = SCHEMAS / schema_name
        if not path.is_file():
            raise ValueError(f"manifest schema does not exist: {schema_name}")
        schema = json.loads(path.read_text(encoding="utf-8"))
        if schema.get("$id") != base_uri.rstrip("/") + "/" + schema_name:
            raise ValueError(f"{schema_name} $id must match manifest schema_base_uri")
        definitions = schema.get("$defs")
        if not isinstance(definitions, dict):
            raise ValueError(f"{schema_name} must contain object $defs")

        for definition_name in sorted(definitions):
            definition = definitions[definition_name]
            if not (
                isinstance(definition, dict)
                and definition.get("type") == "object"
                and definition.get("additionalProperties") is False
            ):
                continue
            properties = definition.get("properties", {})
            required = definition.get("required", [])
            if not isinstance(properties, dict):
                raise ValueError(
                    f"{schema_name}#/$defs/{definition_name} properties must be an object"
                )
            if (
                not isinstance(required, list)
                or any(not isinstance(item, str) or not item for item in required)
                or len(required) != len(set(required))
            ):
                raise ValueError(
                    f"{schema_name}#/$defs/{definition_name} required must be unique text"
                )
            if not set(required).issubset(properties):
                raise ValueError(
                    f"{schema_name}#/$defs/{definition_name} required member lacks property"
                )

            enums: dict[str, list[str]] = {}
            for property_name in sorted(properties):
                property_schema = properties[property_name]
                if not isinstance(property_name, str) or not property_name:
                    raise ValueError("contract property names must be non-empty text")
                if not isinstance(property_schema, dict):
                    raise ValueError(
                        f"{schema_name}#/$defs/{definition_name}.{property_name} "
                        "must be a schema object"
                    )
                enum = property_schema.get("enum")
                if enum is None:
                    continue
                if (
                    not isinstance(enum, list)
                    or not enum
                    or any(not isinstance(item, str) for item in enum)
                    or len(enum) != len(set(enum))
                ):
                    raise ValueError(
                        f"{schema_name}#/$defs/{definition_name}.{property_name} "
                        "direct enum must contain unique strings"
                    )
                enums[property_name] = list(enum)

            contract_name = f"{schema_name}#/$defs/{definition_name}"
            shapes[contract_name] = {
                "allowed": sorted(properties),
                "required": sorted(required),
                "enums": enums,
            }

    if not shapes:
        raise ValueError("no additionalProperties=false object definitions found")
    return version, dict(sorted(shapes.items()))


def _baseline_value(shape: dict[str, object]) -> dict[str, object]:
    required = shape["required"]
    enums = shape["enums"]
    assert isinstance(required, list)
    assert isinstance(enums, dict)
    value: dict[str, object] = {}
    for field in required:
        assert isinstance(field, str)
        allowed = enums.get(field)
        value[field] = allowed[0] if isinstance(allowed, list) and allowed else None
    return value


def generated_corpus(
    version: str,
    shapes: dict[str, dict[str, object]],
) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    names: set[str] = set()

    def add(name: str, contract: str, value: object, expected: bool, dimension: str) -> None:
        if name in names:
            raise ValueError(f"duplicate generated shape case name: {name}")
        names.add(name)
        cases.append(
            {
                "name": name,
                "contract": contract,
                "dimension": dimension,
                "value": value,
                "expected": expected,
            }
        )

    for index, (contract, shape) in enumerate(shapes.items(), start=1):
        prefix = f"shape-{index:03d}"
        baseline = _baseline_value(shape)
        add(prefix + "-valid-minimal", contract, baseline, True, "shape")

        allowed = shape["allowed"]
        required = shape["required"]
        enums = shape["enums"]
        assert isinstance(allowed, list)
        assert isinstance(required, list)
        assert isinstance(enums, dict)

        unknown = "__autotrade_unknown__"
        while unknown in allowed:
            unknown += "_"
        unknown_value = dict(baseline)
        unknown_value[unknown] = None
        add(prefix + "-unknown-field", contract, unknown_value, False, "unknown-field")

        if required:
            missing_value = dict(baseline)
            missing_value.pop(required[0])
            add(
                prefix + "-missing-required",
                contract,
                missing_value,
                False,
                "required-field",
            )

        for enum_index, (field, values) in enumerate(sorted(enums.items()), start=1):
            assert isinstance(values, list) and values
            valid_enum = dict(baseline)
            valid_enum[field] = values[0]
            add(
                f"{prefix}-enum-{enum_index:02d}-valid",
                contract,
                valid_enum,
                True,
                "enum",
            )
            invalid = "__AUTOTRADE_INVALID_ENUM__"
            while invalid in values:
                invalid += "_"
            invalid_enum = dict(baseline)
            invalid_enum[field] = invalid
            add(
                f"{prefix}-enum-{enum_index:02d}-invalid",
                contract,
                invalid_enum,
                False,
                "enum",
            )

    return {
        "contract_version": version,
        "scope": "closed-object-shape-subset",
        "definition_count": len(shapes),
        "case_count": len(cases),
        "cases": cases,
    }


def _descriptor_text(shapes: dict[str, dict[str, object]]) -> str:
    return json.dumps(
        shapes,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def render_python(version: str, descriptor: str) -> str:
    return f'''"""AUTO-GENERATED closed-object contract-shape binding. DO NOT EDIT."""

from __future__ import annotations

import json
from typing import Any

CONTRACT_VERSION = {version!r}
_SHAPES = json.loads({descriptor!r})


def is_valid_contract_shape(contract: str, value: Any) -> bool:
    """Validate only closed-object fields/required/direct-enum semantics."""

    if type(contract) is not str:
        raise ValueError("contract must be exact string")
    shape = _SHAPES.get(contract)
    if shape is None:
        raise ValueError(f"unsupported closed-object contract: {{contract}}")
    if type(value) is not dict:
        return False
    if any(type(key) is not str for key in value):
        return False

    keys = set(value)
    if not set(shape["required"]).issubset(keys):
        return False
    if not keys.issubset(set(shape["allowed"])):
        return False
    for field, allowed in shape["enums"].items():
        if field in value:
            candidate = value[field]
            if type(candidate) is not str or candidate not in allowed:
                return False
    return True
'''


def render_typescript_decl(version: str) -> str:
    return f'''// AUTO-GENERATED closed-object contract-shape binding. DO NOT EDIT.
export declare const CONTRACT_VERSION: "{version}";
export declare function isValidContractShape(
  contract: string,
  value: unknown
): boolean;
'''


def render_typescript_runtime(version: str, descriptor: str) -> str:
    encoded = json.dumps(descriptor, ensure_ascii=True)
    return f'''"use strict";

// AUTO-GENERATED closed-object contract-shape binding. DO NOT EDIT.
const CONTRACT_VERSION = "{version}";
const SHAPES = JSON.parse({encoded});

function isPlainRecord(value) {{
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}}

function isValidContractShape(contract, value) {{
  if (typeof contract !== "string") throw new TypeError("contract must be string");
  const shape = SHAPES[contract];
  if (!shape) throw new RangeError("unsupported closed-object contract: " + contract);
  if (!isPlainRecord(value)) return false;

  const keys = Object.keys(value);
  const observed = new Set(keys);
  for (const required of shape.required) {{
    if (!observed.has(required)) return false;
  }}
  const allowed = new Set(shape.allowed);
  for (const key of keys) {{
    if (!allowed.has(key)) return false;
  }}
  for (const [field, values] of Object.entries(shape.enums)) {{
    if (Object.prototype.hasOwnProperty.call(value, field)) {{
      const candidate = value[field];
      if (typeof candidate !== "string" || !values.includes(candidate)) return false;
    }}
  }}
  return true;
}}

module.exports = {{ CONTRACT_VERSION, isValidContractShape }};
'''


def render_csharp(version: str, descriptor: str) -> str:
    if '"""' in descriptor:
        raise ValueError("generated descriptor cannot be represented as C# raw string")
    return f'''using System.Text.Json;

namespace AutoTrade.Contracts;

/// <summary>
/// AUTO-GENERATED closed-object contract-shape binding.
/// Full JSON Schema validation remains authoritative.
/// </summary>
public static class ContractShapeContracts
{{
    /// <summary>Canonical contract version used to generate this binding.</summary>
    public const string Version = "{version}";

    private const string ShapeJson = """{descriptor}""";
    private static readonly JsonDocument Shapes = JsonDocument.Parse(ShapeJson);

    /// <summary>
    /// Validate only additionalProperties=false, required-member and direct-enum semantics.
    /// </summary>
    public static bool IsValid(string contractName, JsonElement value)
    {{
        if (string.IsNullOrEmpty(contractName))
        {{
            throw new ArgumentException("contractName is required", nameof(contractName));
        }}
        if (!Shapes.RootElement.TryGetProperty(contractName, out var shape))
        {{
            throw new ArgumentOutOfRangeException(
                nameof(contractName),
                contractName,
                "Unsupported closed-object contract."
            );
        }}
        if (value.ValueKind != JsonValueKind.Object)
        {{
            return false;
        }}

        var allowed = StringSet(shape.GetProperty("allowed"));
        var required = StringSet(shape.GetProperty("required"));
        var observed = new HashSet<string>(StringComparer.Ordinal);
        var enums = shape.GetProperty("enums");

        foreach (var property in value.EnumerateObject())
        {{
            observed.Add(property.Name);
            if (!allowed.Contains(property.Name))
            {{
                return false;
            }}
            if (enums.TryGetProperty(property.Name, out var enumValues))
            {{
                if (
                    property.Value.ValueKind != JsonValueKind.String
                    || !ContainsString(enumValues, property.Value.GetString())
                )
                {{
                    return false;
                }}
            }}
        }}

        return required.IsSubsetOf(observed);
    }}

    private static HashSet<string> StringSet(JsonElement values)
    {{
        var result = new HashSet<string>(StringComparer.Ordinal);
        foreach (var value in values.EnumerateArray())
        {{
            var text = value.GetString();
            if (text is null)
            {{
                throw new InvalidOperationException("Generated shape values must be strings.");
            }}
            result.Add(text);
        }}
        return result;
    }}

    private static bool ContainsString(JsonElement values, string? candidate)
    {{
        if (candidate is null)
        {{
            return false;
        }}
        foreach (var value in values.EnumerateArray())
        {{
            if (string.Equals(value.GetString(), candidate, StringComparison.Ordinal))
            {{
                return true;
            }}
        }}
        return false;
    }}
}}
'''


def rendered_outputs() -> dict[str, str]:
    version, shapes = load_shapes()
    descriptor = _descriptor_text(shapes)
    corpus = generated_corpus(version, shapes)
    return {
        "python": render_python(version, descriptor),
        "typescript_decl": render_typescript_decl(version),
        "typescript_runtime": render_typescript_runtime(version, descriptor),
        "csharp": render_csharp(version, descriptor),
        "corpus": json.dumps(
            corpus,
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
        )
        + "\n",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_outputs()
    stale: list[Path] = []

    for key, path in OUTPUTS.items():
        expected = rendered[key]
        if args.check:
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                stale.append(path)
                continue
            if current != expected:
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8", newline="\n")

    if args.check:
        if stale:
            for path in stale:
                print(
                    f"generated contract-shape artifact is stale: {path.relative_to(ROOT)}",
                    file=sys.stderr,
                )
            print(
                "run python tools/generate_contract_shape_bindings.py",
                file=sys.stderr,
            )
            return 1
        print("Contract-shape bindings and corpus are schema-derived and current.")
        return 0

    for path in OUTPUTS.values():
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
