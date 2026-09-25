from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tarfile


SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
ANNOTATION_KEYS = frozenset({"title", "description", "$comment", "examples"})


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())


def load_manifest(root: Path) -> dict:
    return json.loads((root / "contracts" / "manifest.json").read_text(encoding="utf-8"))


def schema_definitions(root: Path, names: list[str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name in names:
        path = root / "contracts" / "jsonschema" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        defs = payload.get("$defs", {})
        if not isinstance(defs, dict):
            raise ValueError(f"{name} has invalid $defs")
        result[name] = defs
    return result


def contract_bytes(root: Path) -> dict[str, bytes]:
    base = root / "contracts"
    files: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _semantic_shape(value: object, *, schema_base_uri: str) -> object:
    """Normalize version-only references and ignore documentation annotations.

    Existing definitions are treated as compatible across a non-major release
    only when their validation semantics are unchanged. Versioned local schema
    URIs naturally move with contract_version, so they are normalized before
    comparison. Human-facing annotations do not affect admission semantics.
    """

    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, child in value.items():
            if key in ANNOTATION_KEYS:
                continue
            normalized[key] = _semantic_shape(child, schema_base_uri=schema_base_uri)
        return normalized
    if isinstance(value, list):
        return [_semantic_shape(child, schema_base_uri=schema_base_uri) for child in value]
    if isinstance(value, str) and schema_base_uri and value.startswith(schema_base_uri):
        return "{SCHEMA_BASE}/" + value[len(schema_base_uri):].lstrip("/")
    return value


def changed_existing_definitions(
    base_root: Path,
    current_root: Path,
    schema_names: list[str],
    *,
    base_uri: str,
    current_uri: str,
) -> dict[str, list[str]]:
    base_defs = schema_definitions(base_root, schema_names)
    current_defs = schema_definitions(current_root, schema_names)
    changed: dict[str, list[str]] = {}
    for schema_name in schema_names:
        common_defs = sorted(set(base_defs[schema_name]) & set(current_defs[schema_name]))
        names = [
            name
            for name in common_defs
            if _semantic_shape(base_defs[schema_name][name], schema_base_uri=base_uri)
            != _semantic_shape(current_defs[schema_name][name], schema_base_uri=current_uri)
        ]
        if names:
            changed[schema_name] = names
    return changed


def evaluate(base_root: Path, current_root: Path) -> list[str]:
    base = load_manifest(base_root)
    current = load_manifest(current_root)
    base_version = parse_version(base["contract_version"])
    current_version = parse_version(current["contract_version"])

    errors: list[str] = []
    if current_version < base_version:
        errors.append("contract_version must never decrease")

    changed = contract_bytes(base_root) != contract_bytes(current_root)
    if changed and current_version == base_version:
        errors.append("contract surface changed without increasing contract_version")

    base_schemas = list(base.get("schemas", []))
    current_schemas = list(current.get("schemas", []))
    removed_schemas = sorted(set(base_schemas) - set(current_schemas))

    common = sorted(set(base_schemas) & set(current_schemas))
    base_defs = schema_definitions(base_root, common)
    current_defs = schema_definitions(current_root, common)
    removed_defs = {
        name: sorted(set(base_defs[name]) - set(current_defs[name]))
        for name in common
        if set(base_defs[name]) - set(current_defs[name])
    }

    breaking_removal = bool(removed_schemas or removed_defs)
    if breaking_removal and current_version[0] <= base_version[0]:
        details = []
        if removed_schemas:
            details.append("removed schemas: " + ", ".join(removed_schemas))
        for name, defs in removed_defs.items():
            details.append(f"removed definitions from {name}: " + ", ".join(defs))
        errors.append("breaking contract removal requires a new major version; " + "; ".join(details))

    changed_defs = changed_existing_definitions(
        base_root,
        current_root,
        common,
        base_uri=str(base.get("schema_base_uri", "")),
        current_uri=str(current.get("schema_base_uri", "")),
    )
    if changed_defs and current_version[0] <= base_version[0]:
        details = "; ".join(
            f"{name}: {', '.join(defs)}" for name, defs in sorted(changed_defs.items())
        )
        errors.append(
            "changed existing contract definition requires a new major version "
            "unless compatibility is proved by a dedicated migration; " + details
        )

    return errors


def export_ref(ref: str) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="autotrade-contract-base-"))
    archive = subprocess.run(
        ["git", "archive", ref],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(temporary, filter="data")
    return temporary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", required=True)
    args = parser.parse_args()
    base = export_ref(args.base_ref)
    errors = evaluate(base, Path.cwd())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Contract version guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
