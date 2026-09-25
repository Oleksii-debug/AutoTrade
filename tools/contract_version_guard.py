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


def _is_transient_runtime_artifact(path: Path, *, base: Path) -> bool:
    """Exclude interpreter output that is not part of the committed contract surface."""

    relative = path.relative_to(base)
    return "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}


def contract_bytes(root: Path) -> dict[str, bytes]:
    base = root / "contracts"
    files: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file() and not _is_transient_runtime_artifact(path, base=base):
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

    breaking_removal = bool(removed_schemas or removed_defs)
    if breaking_removal and current_version[0] <= base_version[0]:
        details = []
        if removed_schemas:
            details.append("removed schemas: " + ", ".join(removed_schemas))
        for name, defs in removed_defs.items():
            details.append(f"removed definitions from {name}: " + ", ".join(defs))
        errors.append("breaking contract removal requires a new major version; " + "; ".join(details))

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
