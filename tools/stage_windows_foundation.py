"""Stage mandatory source-controlled AutoTrade foundation into Windows composition.

The Windows bundle builder intentionally verifies an already prepared staging
root. This module is the canonical first-party assembly step for the neutral
Python foundation required by durable product runtime components.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sys


ROOT = Path(__file__).resolve().parents[1]
_REQUIRED = (
    (
        "autotrade-foundation-package",
        "runtime-foundation",
        "autotrade_foundation/__init__.py",
    ),
    (
        "autotrade-foundation-local-filesystem",
        "runtime-foundation",
        "autotrade_foundation/local_filesystem.py",
    ),
)


class FoundationStagingError(ValueError):
    pass


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _source_bytes(relative: str, *, source_root: Path) -> bytes:
    pure = PurePosixPath(relative)
    path = source_root.joinpath(*pure.parts)
    if path.is_symlink() or not path.is_file():
        raise FoundationStagingError(
            f"required foundation source is missing or unsafe: {relative}"
        )
    return path.read_bytes()


def _stage_exact(staging: Path, relative: str, data: bytes) -> None:
    destination = staging.joinpath(*PurePosixPath(relative).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise FoundationStagingError(
            f"foundation staging destination is a symlink: {relative}"
        )
    if destination.exists():
        if not destination.is_file():
            raise FoundationStagingError(
                f"foundation staging destination is not a file: {relative}"
            )
        if destination.read_bytes() != data:
            raise FoundationStagingError(
                f"foundation staging destination has conflicting bytes: {relative}"
            )
        return
    destination.write_bytes(data)
    if destination.read_bytes() != data:
        raise FoundationStagingError(
            f"foundation staging write verification failed: {relative}"
        )


def stage_windows_foundation(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path = ROOT,
) -> tuple[dict[str, str], ...]:
    """Stage mandatory foundation bytes and bind them into composition authority."""

    if not staging.is_dir():
        raise FoundationStagingError("staging must be an existing directory")
    try:
        composition = json.loads(composition_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FoundationStagingError("composition manifest is missing or invalid") from error
    if not isinstance(composition, dict):
        raise FoundationStagingError("composition manifest must be an object")
    components = composition.get("components")
    if not isinstance(components, list):
        raise FoundationStagingError("composition components must be an array")

    existing_by_path: dict[str, dict[str, object]] = {}
    existing_ids: set[str] = set()
    for item in components:
        if not isinstance(item, dict):
            raise FoundationStagingError("composition component must be an object")
        path = item.get("path")
        component_id = item.get("component_id")
        if not isinstance(path, str) or not isinstance(component_id, str):
            raise FoundationStagingError("composition component identity is invalid")
        if path in existing_by_path or component_id in existing_ids:
            raise FoundationStagingError("composition component identity is duplicated")
        existing_by_path[path] = item
        existing_ids.add(component_id)

    staged: list[dict[str, str]] = []
    for component_id, kind, relative in _REQUIRED:
        data = _source_bytes(relative, source_root=source_root)
        _stage_exact(staging, relative, data)
        digest = "sha256:" + sha256(data).hexdigest()
        expected = {
            "component_id": component_id,
            "kind": kind,
            "path": relative,
            "version": "source-controlled",
            "sha256": digest,
        }
        existing = existing_by_path.get(relative)
        if existing is not None:
            if existing != expected:
                raise FoundationStagingError(
                    f"composition has conflicting foundation component: {relative}"
                )
        else:
            if component_id in existing_ids:
                raise FoundationStagingError(
                    f"composition foundation component_id conflicts: {component_id}"
                )
            components.append(expected)
            existing_by_path[relative] = expected
            existing_ids.add(component_id)
        staged.append(expected)

    components.sort(key=lambda item: str(item.get("path", "")))
    composition_path.write_bytes(_canonical_json(composition))
    return tuple(staged)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--composition", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = stage_windows_foundation(
            staging=args.staging,
            composition_path=args.composition,
        )
    except FoundationStagingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
