"""Generate drift-free common scalar bindings from canonical JSON Schema.

The generator intentionally covers only primitive definitions whose semantics are
fully expressible as a string regex/length constraint or a closed string enum.
Format-bearing values (for example UUID/date-time) and object definitions remain
under their dedicated contract validators rather than being approximated here.

CI runs --check so Python, C# and TypeScript cannot silently diverge from
common.schema.json or contracts/manifest.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "contracts" / "jsonschema" / "common.schema.json"
MANIFEST = ROOT / "contracts" / "manifest.json"
OUTPUTS = {
    "python": ROOT / "contracts" / "bindings" / "python" / "common_scalars.py",
    "typescript_decl": ROOT / "contracts" / "bindings" / "typescript" / "commonScalars.d.ts",
    "typescript_runtime": ROOT / "contracts" / "bindings" / "typescript" / "commonScalars.js",
    "csharp": ROOT / "src" / "AutoTrade.Contracts" / "CommonScalarContracts.cs",
}


def _load_source() -> tuple[str, dict[str, dict[str, object]]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    common = json.loads(COMMON.read_text(encoding="utf-8"))
    version = manifest.get("contract_version")
    base_uri = manifest.get("schema_base_uri")
    if not isinstance(version, str) or not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version
    ):
        raise ValueError("manifest contract_version must be semantic version text")
    if not isinstance(base_uri, str) or not base_uri:
        raise ValueError("manifest schema_base_uri must be non-empty text")
    path_version = urlsplit(base_uri).path.rstrip("/").split("/")[-1]
    if path_version != version:
        raise ValueError("schema_base_uri version must match contract_version")
    if common.get("$id") != base_uri.rstrip("/") + "/common.schema.json":
        raise ValueError("common schema $id must match manifest schema_base_uri")
    defs = common.get("$defs")
    if not isinstance(defs, dict):
        raise ValueError("common schema $defs must be an object")

    selected: dict[str, dict[str, object]] = {}
    for name, definition in defs.items():
        if not isinstance(name, str) or not isinstance(definition, dict):
            raise ValueError("common schema definitions must be named objects")
        if "format" in definition:
            continue
        enum = definition.get("enum")
        pattern = definition.get("pattern")
        if enum is not None:
            if (
                not isinstance(enum, list)
                or not enum
                or any(not isinstance(item, str) for item in enum)
            ):
                raise ValueError(f"{name} enum must contain string values")
            selected[name] = definition
            continue
        if definition.get("type") == "string" and isinstance(pattern, str):
            selected[name] = definition

    if not selected:
        raise ValueError("no mechanically generatable common scalar definitions found")
    return version, selected


def _limits(definition: dict[str, object]) -> tuple[int | None, int | None]:
    minimum = definition.get("minLength")
    maximum = definition.get("maxLength")
    if minimum is not None and (type(minimum) is not int or minimum < 0):
        raise ValueError("minLength must be a non-negative integer")
    if maximum is not None and (type(maximum) is not int or maximum < 0):
        raise ValueError("maxLength must be a non-negative integer")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minLength cannot exceed maxLength")
    return minimum, maximum


def _ordered(defs: dict[str, dict[str, object]]) -> list[str]:
    preferred = ["Decimal", "Sequence", "Digest", "CurrencyId", "UnitId", "Environment"]
    return [name for name in preferred if name in defs] + sorted(
        name for name in defs if name not in preferred
    )


def render_python(version: str, defs: dict[str, dict[str, object]]) -> str:
    names = _ordered(defs)
    pattern_names = [name for name in names if isinstance(defs[name].get("pattern"), str)]
    enum_names = [name for name in names if isinstance(defs[name].get("enum"), list)]
    lines = [
        '"""AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.\n\n'
        "Run python tools/generate_common_scalar_bindings.py to regenerate.\n"
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import re",
        "",
        f'CONTRACT_VERSION = "{version}"',
        "",
        "_PATTERNS = {",
    ]
    for name in pattern_names:
        lines.append(f'    "{name}": re.compile({defs[name]["pattern"]!r}),')
    lines += ["}", "_LENGTHS = {"]
    for name in pattern_names:
        minimum, maximum = _limits(defs[name])
        if minimum is not None or maximum is not None:
            lines.append(f'    "{name}": ({minimum!r}, {maximum!r}),')
    lines += ["}", "_ENUMS = {"]
    for name in enum_names:
        values = ", ".join(repr(value) for value in defs[name]["enum"])
        lines.append(f'    "{name}": frozenset(({values},)),')
    lines += [
        "}",
        "",
        "",
        "def is_valid_common_scalar(kind: str, value: object) -> bool:",
        "    if not isinstance(value, str):",
        "        return False",
        "    enum = _ENUMS.get(kind)",
        "    if enum is not None:",
        "        return value in enum",
        "    pattern = _PATTERNS.get(kind)",
        "    if pattern is None:",
        '        raise ValueError(f"unsupported common scalar kind: {kind}")',
        "    limits = _LENGTHS.get(kind)",
        "    if limits is not None:",
        "        minimum, maximum = limits",
        "        if minimum is not None and len(value) < minimum:",
        "            return False",
        "        if maximum is not None and len(value) > maximum:",
        "            return False",
        "    return pattern.fullmatch(value) is not None",
        "",
    ]
    return "\n".join(lines)


def _js_regex(pattern: str) -> str:
    return pattern.replace("/", r"\/")


def render_typescript_runtime(version: str, defs: dict[str, dict[str, object]]) -> str:
    names = _ordered(defs)
    pattern_names = [name for name in names if isinstance(defs[name].get("pattern"), str)]
    enum_names = [name for name in names if isinstance(defs[name].get("enum"), list)]
    lines = [
        '"use strict";',
        "",
        "// AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.",
        "// Run python tools/generate_common_scalar_bindings.py to regenerate.",
        f'const CONTRACT_VERSION = "{version}";',
        "",
        "const patterns = Object.freeze({",
    ]
    for name in pattern_names:
        lines.append(f"  {name}: /{_js_regex(defs[name]['pattern'])}/,")
    lines += ["});", "const lengths = Object.freeze({"]
    for name in pattern_names:
        minimum, maximum = _limits(defs[name])
        if minimum is not None or maximum is not None:
            min_js = "null" if minimum is None else str(minimum)
            max_js = "null" if maximum is None else str(maximum)
            lines.append(f"  {name}: Object.freeze([{min_js}, {max_js}]),")
    lines += ["});", "const enums = Object.freeze({"]
    for name in enum_names:
        values = ", ".join(json.dumps(v) for v in defs[name]["enum"])
        lines.append(f"  {name}: new Set([{values}]),")
    lines += [
        "});",
        "",
        "function isValidCommonScalar(kind, value) {",
        '  if (typeof value !== "string") return false;',
        "  const enumValues = enums[kind];",
        "  if (enumValues) return enumValues.has(value);",
        "  const pattern = patterns[kind];",
        '  if (!pattern) throw new RangeError("unsupported common scalar kind: " + kind);',
        "  const limit = lengths[kind];",
        "  if (limit) {",
        "    const [minimum, maximum] = limit;",
        "    if (minimum !== null && value.length < minimum) return false;",
        "    if (maximum !== null && value.length > maximum) return false;",
        "  }",
        "  return pattern.test(value);",
        "}",
        "",
        "module.exports = { CONTRACT_VERSION, isValidCommonScalar };",
        "",
    ]
    return "\n".join(lines)


def render_typescript_decl(version: str, defs: dict[str, dict[str, object]]) -> str:
    names = _ordered(defs)
    union = "\n".join(
        f'  {"|" if index else " "} "{name}"'
        for index, name in enumerate(names)
    )
    return (
        "// AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.\n"
        "// Run python tools/generate_common_scalar_bindings.py to regenerate.\n"
        f'export declare const CONTRACT_VERSION: "{version}";\n\n'
        "export type CommonScalarKind =\n"
        f"{union};\n\n"
        "export declare function isValidCommonScalar(\n"
        "  kind: CommonScalarKind,\n"
        "  value: unknown\n"
        "): boolean;\n"
    )


def _csharp_regex_method(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name) + "Pattern"


def render_csharp(version: str, defs: dict[str, dict[str, object]]) -> str:
    del version
    names = _ordered(defs)
    pattern_names = [name for name in names if isinstance(defs[name].get("pattern"), str)]
    enum_names = [name for name in names if isinstance(defs[name].get("enum"), list)]
    lines = [
        "using System.Text.RegularExpressions;",
        "",
        "namespace AutoTrade.Contracts;",
        "",
        "/// <summary>",
        "/// AUTO-GENERATED strict admission for the language-neutral common scalar subset.",
        "/// Run python tools/generate_common_scalar_bindings.py to regenerate.",
        "/// </summary>",
        "public static partial class CommonScalarContracts",
        "{",
    ]
    for name in enum_names:
        values = ", ".join(f'\"{value}\"' for value in defs[name]["enum"])
        lines += [
            f"    private static readonly HashSet<string> {name}Values =",
            f"        new(StringComparer.Ordinal) {{ {values} }};",
            "",
        ]
    lines += [
        "    /// <summary>Validates a textual common scalar without numeric coercion.</summary>",
        "    public static bool IsValid(string kind, string? value)",
        "    {",
        "        if (value is null)",
        "        {",
        "            return false;",
        "        }",
        "",
        "        return kind switch",
        "        {",
    ]
    for name in names:
        definition = defs[name]
        if isinstance(definition.get("enum"), list):
            lines.append(f'            "{name}" => {name}Values.Contains(value),')
            continue
        minimum, maximum = _limits(definition)
        checks: list[str] = []
        if minimum is not None:
            checks.append(f"value.Length >= {minimum}")
        if maximum is not None:
            checks.append(f"value.Length <= {maximum}")
        checks.append(f"{_csharp_regex_method(name)}().IsMatch(value)")
        lines.append(f'            "{name}" => ' + " && ".join(checks) + ",")
    lines += [
        '            _ => throw new ArgumentOutOfRangeException(nameof(kind), kind, "Unsupported common scalar kind."),',
        "        };",
        "    }",
        "",
    ]
    for name in pattern_names:
        pattern = defs[name]["pattern"].replace('"', '""')
        lines += [
            f'    [GeneratedRegex(@"{pattern}", RegexOptions.CultureInvariant)]',
            f"    private static partial Regex {_csharp_regex_method(name)}();",
            "",
        ]
    lines += ["}", ""]
    return "\n".join(lines)


def rendered_outputs() -> dict[str, str]:
    version, defs = _load_source()
    return {
        "python": render_python(version, defs),
        "typescript_decl": render_typescript_decl(version, defs),
        "typescript_runtime": render_typescript_runtime(version, defs),
        "csharp": render_csharp(version, defs),
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
            path.write_text(expected, encoding="utf-8")

    if args.check:
        if stale:
            for path in stale:
                print(
                    f"generated common scalar binding is stale: {path.relative_to(ROOT)}",
                    file=sys.stderr,
                )
            print(
                "run python tools/generate_common_scalar_bindings.py",
                file=sys.stderr,
            )
            return 1
        print("Common scalar bindings are schema-derived and current.")
        return 0

    for path in OUTPUTS.values():
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
