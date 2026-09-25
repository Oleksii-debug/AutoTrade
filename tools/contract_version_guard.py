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


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())


def load_manifest(root: Path) -> dict:
    return json.loads((root / "contracts" / "manifest.json").read_text(encoding="utf-8"))


def schema_definitions(root: Path, names: list[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for name in names:
        path = root / "contracts" / "jsonschema" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        defs = payload.get("$defs", {})
        if not isinstance(defs, dict):
            raise ValueError(f"{name} has invalid $defs")
        result[name] = set(defs)
    return result


def schema_required_members(
    root: Path,
    names: list[str],
) -> dict[str, dict[str, set[str]]]:
    """Return required-member sets keyed by stable JSON-tree path.

    Only paths present in both contract revisions are compared. A newly added
    definition/object may introduce its own required fields without breaking
    existing instances, while adding a required member to an existing object
    makes previously valid payloads invalid and therefore requires a major bump.
    """

    result: dict[str, dict[str, set[str]]] = {}
    for name in names:
        payload = json.loads(
            (root / "contracts" / "jsonschema" / name).read_text(encoding="utf-8")
        )
        paths: dict[str, set[str]] = {}

        def visit(value: object, path: str) -> None:
            if isinstance(value, dict):
                required = value.get("required", [])
                if (
                    not isinstance(required, list)
                    or any(not isinstance(item, str) or not item for item in required)
                ):
                    raise ValueError(f"{name} has invalid required array at {path}")
                # Record the object path even when required is absent. This
                # compares an existing empty required-set with the same object
                # after a member becomes required. Truly new paths remain
                # excluded by the base/current path intersection below.
                paths[path] = set(required)
                for key, child in value.items():
                    visit(child, f"{path}/{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}/{index}")

        visit(payload, "$")
        result[name] = paths
    return result


def contract_bytes(root: Path) -> dict[str, bytes]:
    base = root / "contracts"
    files: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


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
        name: sorted(base_defs[name] - current_defs[name])
        for name in common
        if base_defs[name] - current_defs[name]
    }

    base_required = schema_required_members(base_root, common)
    current_required = schema_required_members(current_root, common)
    added_required: dict[str, dict[str, list[str]]] = {}
    for name in common:
        common_paths = set(base_required[name]) & set(current_required[name])
        additions = {
            path: sorted(current_required[name][path] - base_required[name][path])
            for path in sorted(common_paths)
            if current_required[name][path] - base_required[name][path]
        }
        if additions:
            added_required[name] = additions

    breaking_change = bool(removed_schemas or removed_defs or added_required)
    if breaking_change and current_version[0] <= base_version[0]:
        details = []
        if removed_schemas:
            details.append("removed schemas: " + ", ".join(removed_schemas))
        for name, defs in removed_defs.items():
            details.append(f"removed definitions from {name}: " + ", ".join(defs))
        for name, paths in added_required.items():
            for path, members in paths.items():
                details.append(
                    f"new required members in {name} at {path}: " + ", ".join(members)
                )
        errors.append("breaking contract change requires a new major version; " + "; ".join(details))

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


def evaluate_refs(base_ref: str, current_ref: str = "HEAD") -> list[str]:
    """Compare committed contract trees, never test-mutated working-tree bytes."""
    base = export_ref(base_ref)
    current = export_ref(current_ref)
    return evaluate(base, current)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--current-ref", default="HEAD")
    args = parser.parse_args()
    errors = evaluate_refs(args.base_ref, args.current_ref)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Contract version guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
