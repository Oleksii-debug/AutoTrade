"""Generate the reviewed common-scalar conformance corpus from canonical JSON Schema.

Candidate values are explicit review vectors, but acceptance is derived from
common.schema.json. CI runs --check so schema semantics cannot drift away from
the Python/C#/TypeScript corpus without detection.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "contracts" / "jsonschema" / "common.schema.json"
MANIFEST = ROOT / "contracts" / "manifest.json"
CORPUS = ROOT / "contracts" / "fixtures" / "common-scalars.corpus.json"
DECIMAL_ENVELOPE_KEY = "x-autotrade-decimal-envelope"


CANDIDATES: tuple[tuple[str, str, object], ...] = (
    ("decimal-zero", "Decimal", "0"),
    ("decimal-integer", "Decimal", "123"),
    ("decimal-fraction", "Decimal", "-0.125"),
    ("decimal-trailing-zero", "Decimal", "1.20"),
    ("decimal-exponent", "Decimal", "1e3"),
    ("decimal-negative-zero", "Decimal", "-0"),
    ("decimal-leading-plus", "Decimal", "+1"),
    ("decimal-final-lf", "Decimal", "1\n"),
    ("decimal-final-crlf", "Decimal", "1\r\n"),
    (
        "decimal-significant-at-limit",
        "Decimal",
        "9" * 128 + "." + "9" * 128,
    ),
    (
        "decimal-significant-over-limit",
        "Decimal",
        "9" * 129 + "." + "9" * 128,
    ),
    ("decimal-integer-at-limit", "Decimal", "9" * 256),
    ("decimal-integer-over-limit", "Decimal", "9" * 257),
    ("decimal-scale-at-limit", "Decimal", "0." + "0" * 255 + "1"),
    ("decimal-scale-over-limit", "Decimal", "0." + "0" * 256 + "1"),
    (
        "decimal-mixed-significant-over-limit",
        "Decimal",
        "9" * 129 + "." + "9" * 128,
    ),
    ("decimal-negative-integer-at-limit", "Decimal", "-" + "9" * 256),
    (
        "decimal-negative-scale-at-limit",
        "Decimal",
        "-0." + "0" * 255 + "1",
    ),
    (
        "decimal-raw-length-at-limit",
        "Decimal",
        "-0." + "0" * 255 + "1",
    ),
    (
        "decimal-raw-length-over-limit",
        "Decimal",
        "-0." + "0" * 256 + "1",
    ),
    ("sequence-zero", "Sequence", "0"),
    ("sequence-large", "Sequence", "184467440737095516160000"),
    ("sequence-leading-zero", "Sequence", "01"),
    ("sequence-negative", "Sequence", "-1"),
    ("sequence-exponent", "Sequence", "1e3"),
    ("sequence-final-lf", "Sequence", "1\n"),
    ("sequence-final-crlf", "Sequence", "1\r\n"),
    ("digest-valid", "Digest", "sha256:" + "a" * 64),
    ("digest-uppercase", "Digest", "sha256:" + "A" * 64),
    ("digest-wrong-length", "Digest", "sha256:" + "a" * 63),
    ("digest-final-lf", "Digest", "sha256:" + "a" * 64 + "\n"),
    ("digest-final-crlf", "Digest", "sha256:" + "a" * 64 + "\r\n"),
    ("environment-live", "Environment", "LIVE"),
    ("environment-paper", "Environment", "PAPER"),
    ("environment-unknown", "Environment", "PRODUCTION"),
    ("environment-case", "Environment", "live"),
    ("currency-simple", "CurrencyId", "USD"),
    ("currency-qualified", "CurrencyId", "USDC.native:erc20"),
    ("currency-space", "CurrencyId", "US D"),
    ("currency-empty", "CurrencyId", ""),
    ("currency-final-lf", "CurrencyId", "USD\n"),
    ("currency-final-crlf", "CurrencyId", "USD\r\n"),
    ("unit-simple", "UnitId", "shares"),
    ("unit-path", "UnitId", "contract/BTC-USD"),
    ("unit-space", "UnitId", "base units"),
    ("unit-too-long", "UnitId", "x" * 65),
    ("unit-final-lf", "UnitId", "shares\n"),
    ("unit-final-crlf", "UnitId", "shares\r\n"),
    ("decimal-json-number", "Decimal", 1.25),
    ("sequence-json-number", "Sequence", 1),
    ("digest-json-null", "Digest", None),
)


def _decimal_envelope(definition: dict[str, object]) -> tuple[int, int, int]:
    raw = definition.get(DECIMAL_ENVELOPE_KEY)
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {"max_significant_digits", "max_scale", "max_integer_digits"}
    ):
        raise ValueError("Decimal resource envelope metadata is missing or invalid")
    values = (
        raw["max_significant_digits"],
        raw["max_scale"],
        raw["max_integer_digits"],
    )
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("Decimal resource envelope limits must be positive integers")
    return values  # type: ignore[return-value]


def _within_decimal_geometry(
    value: object,
    limits: tuple[int, int, int],
) -> bool:
    """Mirror generated binding geometry independently of schema extraction.

    Zero has no integer magnitude, but its coefficient has one significant
    digit. Keep this oracle independent of contract/runtime binding imports.
    """
    if type(value) is not str:
        return False
    max_significant_digits, max_scale, max_integer_digits = limits
    unsigned = value[1:] if value.startswith("-") else value
    integer_part, dot, fractional_part = unsigned.partition(".")
    integer_magnitude = 0 if integer_part == "0" else len(integer_part)
    coefficient = integer_part + fractional_part
    significant_digits = len(coefficient.lstrip("0")) or 1
    return (
        significant_digits <= max_significant_digits
        and (len(fractional_part) if dot else 0) <= max_scale
        and integer_magnitude <= max_integer_digits
    )


def _within_decimal_envelope(
    value: object,
    definition: dict[str, object],
) -> bool:
    if type(value) is not str:
        return False
    return _within_decimal_geometry(value, _decimal_envelope(definition))


def generated_document() -> dict[str, object]:
    common = json.loads(COMMON.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    contract_version = manifest["contract_version"]
    schema_base_uri = manifest["schema_base_uri"]
    base_path_version = urlsplit(schema_base_uri).path.rstrip("/").split("/")[-1]
    if base_path_version != contract_version:
        raise ValueError("schema_base_uri version must match manifest contract_version")
    expected_schema_id = schema_base_uri.rstrip("/") + "/common.schema.json"
    if common["$id"] != expected_schema_id:
        raise ValueError("common schema $id must match manifest schema_base_uri")

    registry = Registry().with_resource(
        common["$id"],
        Resource.from_contents(common),
    )
    names: set[str] = set()
    cases: list[dict[str, object]] = []
    for name, definition, value in CANDIDATES:
        if name in names:
            raise ValueError(f"duplicate candidate name: {name}")
        names.add(name)
        if definition not in common["$defs"]:
            raise ValueError(f"unknown common scalar definition: {definition}")
        validator = Draft202012Validator(
            {"$ref": f"{common['$id']}#/$defs/{definition}"},
            registry=registry,
        )
        expected = validator.is_valid(value)
        if definition == "Decimal":
            expected = expected and _within_decimal_envelope(
                value,
                common["$defs"]["Decimal"],
            )
        cases.append(
            {
                "name": name,
                "type": definition,
                "value": value,
                "expected": expected,
            }
        )

    return {
        "corpus_version": contract_version,
        "contract_version": contract_version,
        "scope": "common-scalar-subset",
        "cases": cases,
    }


def rendered_document() -> str:
    return json.dumps(generated_document(), indent=2, ensure_ascii=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    rendered = rendered_document()
    if args.check:
        try:
            current = CORPUS.read_text(encoding="utf-8")
        except OSError as error:
            print(f"common scalar corpus is missing: {error}", file=sys.stderr)
            return 2
        if current != rendered:
            print(
                "common scalar corpus is stale; run "
                "python tools/generate_common_scalar_corpus.py",
                file=sys.stderr,
            )
            return 1
        print("Common scalar corpus is schema-derived and current.")
        return 0

    CORPUS.write_text(rendered, encoding="utf-8")
    print(f"Wrote {CORPUS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
