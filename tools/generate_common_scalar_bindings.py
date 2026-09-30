"""Generate drift-free common scalar bindings from canonical JSON Schema.

The generator intentionally covers only primitive definitions whose semantics are
fully expressible as a string regex/length constraint or a closed string enum.
Format-bearing values (for example UUID/date-time) and object definitions remain
under their dedicated contract validators rather than being approximated here.

CI runs --check so Python, C# and TypeScript cannot silently diverge from
common.schema.json or contracts/manifest.json. The shipped MVP Python runtime is
rendered from the exact same source/template as the contract-facing Python
binding so installed product code never depends on the repository contracts tree.
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
    "mvp_python": ROOT / "mvp" / "autotrade_mvp" / "_generated_common_scalars.py",
    "neutral_python": ROOT / "autotrade_numeric" / "_generated_common_scalars.py",
    "typescript_decl": ROOT / "contracts" / "bindings" / "typescript" / "commonScalars.d.ts",
    "typescript_runtime": ROOT / "contracts" / "bindings" / "typescript" / "commonScalars.js",
    "csharp": ROOT / "src" / "AutoTrade.Contracts" / "CommonScalarContracts.cs",
    "mvp_decimal_limits": ROOT / "mvp" / "autotrade_mvp" / "_generated_decimal_limits.py",
    "neutral_decimal_limits": ROOT / "autotrade_numeric" / "_generated_decimal_limits.py",
}

DECIMAL_ENVELOPE_KEY = "x-autotrade-decimal-envelope"
DECIMAL_ENVELOPE_FIELDS = (
    "max_significant_digits",
    "max_scale",
    "max_integer_digits",
)


def _decimal_envelope(
    name: str,
    definition: dict[str, object],
) -> tuple[int, int, int] | None:
    raw = definition.get(DECIMAL_ENVELOPE_KEY)
    if raw is None:
        if name == "Decimal":
            raise ValueError("Decimal must define resource envelope metadata")
        return None
    if (
        name != "Decimal"
        or not isinstance(raw, dict)
        or set(raw) != set(DECIMAL_ENVELOPE_FIELDS)
    ):
        raise ValueError(
            "decimal envelope metadata is valid only on Decimal with canonical fields"
        )
    values = tuple(raw[field] for field in DECIMAL_ENVELOPE_FIELDS)
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("Decimal envelope limits must be positive integers")
    return values  # type: ignore[return-value]


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
    for name, definition in selected.items():
        _decimal_envelope(name, definition)
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


def render_mvp_decimal_limits(
    version: str,
    defs: dict[str, dict[str, object]],
) -> str:
    definition = defs.get("Decimal")
    if definition is None:
        raise ValueError("Decimal definition is required for MVP runtime limits")
    envelope = _decimal_envelope("Decimal", definition)
    if envelope is None:
        raise ValueError("Decimal resource envelope is required for MVP runtime limits")
    _minimum, maximum = _limits(definition)
    if maximum is None:
        raise ValueError("Decimal maxLength is required for MVP runtime limits")
    return "\n".join(
        [
            '"""AUTO-GENERATED Decimal resource limits. DO NOT EDIT.',
            "",
            "Generated from contracts/jsonschema/common.schema.json by",
            "tools/generate_common_scalar_bindings.py.",
            '"""',
            "",
            f'CONTRACT_VERSION = "{version}"',
            f"MAX_DECIMAL_TEXT_LENGTH = {maximum}",
            f"MAX_SIGNIFICANT_DIGITS = {envelope[0]}",
            f"MAX_SCALE = {envelope[1]}",
            f"MAX_INTEGER_DIGITS = {envelope[2]}",
            "",
        ]
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
    lines += ["}", "_DECIMAL_ENVELOPES = {"]
    for name in pattern_names:
        envelope = _decimal_envelope(name, defs[name])
        if envelope is not None:
            lines.append(f'    "{name}": {envelope!r},')
    lines += ["}", "_ENUMS = {"]
    for name in enum_names:
        values = ", ".join(repr(value) for value in defs[name]["enum"])
        lines.append(f'    "{name}": frozenset(({values},)),')
    lines += [
        "}",
        "",
        "",
        "def _within_decimal_envelope(",
        "    value: str, limits: tuple[int, int, int]",
        ") -> bool:",
        "    max_significant_digits, max_scale, max_integer_digits = limits",
        '    unsigned = value[1:] if value.startswith("-") else value',
        '    integer_part, dot, fractional_part = unsigned.partition(".")',
        '    integer_magnitude = 0 if integer_part == "0" else len(integer_part)',
        "    scale = len(fractional_part) if dot else 0",
        "    coefficient = integer_part + fractional_part",
        '    significant_digits = len(coefficient.lstrip("0")) or 1',
        "    return (",
        "        significant_digits <= max_significant_digits",
        "        and scale <= max_scale",
        "        and integer_magnitude <= max_integer_digits",
        "    )",
        "",
        "",
        "def is_valid_common_scalar(kind: str, value: object) -> bool:",
        "    if type(value) is not str:",
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
        "    if pattern.fullmatch(value) is None:",
        "        return False",
        "    decimal_limits = _DECIMAL_ENVELOPES.get(kind)",
        "    if decimal_limits is not None and not _within_decimal_envelope(",
        "        value, decimal_limits",
        "    ):",
        "        return False",
        "    return True",
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
    lines += ["});", "const decimalEnvelopes = Object.freeze({"]
    for name in pattern_names:
        envelope = _decimal_envelope(name, defs[name])
        if envelope is not None:
            lines.append(
                f"  {name}: Object.freeze([{envelope[0]}, {envelope[1]}, {envelope[2]}]),"
            )
    lines += ["});", "const enums = Object.freeze({"]
    for name in enum_names:
        values = ", ".join(json.dumps(v) for v in defs[name]["enum"])
        lines.append(f"  {name}: new Set([{values}]),")
    lines += [
        "});",
        "",
        "function withinDecimalEnvelope(value, limits) {",
        "  const [maxSignificantDigits, maxScale, maxIntegerDigits] = limits;",
        '  const unsigned = value.startsWith("-") ? value.slice(1) : value;',
        '  const dotIndex = unsigned.indexOf(".");',
        '  const integerPart = dotIndex === -1 ? unsigned : unsigned.slice(0, dotIndex);',
        '  const fractionalPart = dotIndex === -1 ? "" : unsigned.slice(dotIndex + 1);',
        '  const integerMagnitude = integerPart === "0" ? 0 : integerPart.length;',
        "  const coefficient = integerPart + fractionalPart;",
        "  const firstNonZero = coefficient.search(/[1-9]/);",
        "  const significantDigits =",
        "    firstNonZero === -1 ? 1 : coefficient.length - firstNonZero;",
        "  return (",
        "    significantDigits <= maxSignificantDigits &&",
        "    fractionalPart.length <= maxScale &&",
        "    integerMagnitude <= maxIntegerDigits",
        "  );",
        "}",
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
        "  const match = pattern.exec(value);",
        "  if (match === null || match.index !== 0 || match[0].length !== value.length) {",
        "    return false;",
        "  }",
        "  const decimalLimits = decimalEnvelopes[kind];",
        "  return !decimalLimits || withinDecimalEnvelope(value, decimalLimits);",
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
        checks.append(f"IsFullMatch({_csharp_regex_method(name)}(), value)")
        envelope = _decimal_envelope(name, definition)
        if envelope is not None:
            checks.append(
                "IsWithinDecimalEnvelope("
                f"value, {envelope[0]}, {envelope[1]}, {envelope[2]})"
            )
        lines.append(f'            "{name}" => ' + " && ".join(checks) + ",")
    lines += [
        '            _ => throw new ArgumentOutOfRangeException(nameof(kind), kind, "Unsupported common scalar kind."),',
        "        };",
        "    }",
        "",
        "    private static bool IsFullMatch(Regex regex, string value)",
        "    {",
        "        var match = regex.Match(value);",
        "        return match.Success && match.Index == 0 && match.Length == value.Length;",
        "    }",
        "",
        "    private static bool IsWithinDecimalEnvelope(",
        "        string value,",
        "        int maxSignificantDigits,",
        "        int maxScale,",
        "        int maxIntegerDigits)",
        "    {",
        '        var start = value.StartsWith("-", StringComparison.Ordinal) ? 1 : 0;',
        "        var dot = value.IndexOf('.', start);",
        "        var integerEnd = dot >= 0 ? dot : value.Length;",
        "        var integerDigits = integerEnd - start;",
        "        var integerMagnitude =",
        "            integerDigits == 1 && value[start] == '0' ? 0 : integerDigits;",
        "        var scale = dot >= 0 ? value.Length - dot - 1 : 0;",
        "        var coefficientDigits = value.Length - start - (dot >= 0 ? 1 : 0);",
        "        var leadingCoefficientZeros = 0;",
        "        for (var index = start; index < value.Length; index++)",
        "        {",
        "            if (value[index] == '.')",
        "            {",
        "                continue;",
        "            }",
        "            if (value[index] != '0')",
        "            {",
        "                break;",
        "            }",
        "            leadingCoefficientZeros++;",
        "        }",
        "        var significantDigits =",
        "            leadingCoefficientZeros == coefficientDigits",
        "                ? 1",
        "                : coefficientDigits - leadingCoefficientZeros;",
        "        return significantDigits <= maxSignificantDigits",
        "            && scale <= maxScale",
        "            && integerMagnitude <= maxIntegerDigits;",
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
    python_runtime = render_python(version, defs)
    return {
        "python": python_runtime,
        "mvp_python": python_runtime,
        "neutral_python": python_runtime,
        "typescript_decl": render_typescript_decl(version, defs),
        "typescript_runtime": render_typescript_runtime(version, defs),
        "csharp": render_csharp(version, defs),
        "mvp_decimal_limits": render_mvp_decimal_limits(version, defs),
        "neutral_decimal_limits": render_mvp_decimal_limits(version, defs),
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
