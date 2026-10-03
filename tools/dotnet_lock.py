from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

EXACT_NUGET_VERSION = re.compile(r"^\[([0-9][A-Za-z0-9.+-]*)\]$")


def _strict_json(text: str):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicates)


def _valid_sha512_content_hash(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 64


def normalized_dotnet_lock(path: Path) -> dict[str, object]:
    """Parse one NuGet lock into the canonical provenance shape, fail closed."""

    try:
        document = _strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"NuGet lock is unreadable: {path}") from error
    if type(document) is not dict or document.get("version") != 1:
        raise ValueError(f"NuGet lock must use schema version 1: {path}")
    if set(document) != {"version", "dependencies"}:
        raise ValueError(f"NuGet lock root has unexpected fields: {path}")
    dependencies = document.get("dependencies")
    if type(dependencies) is not dict:
        raise ValueError(f"NuGet lock dependencies must be an object: {path}")

    normalized_targets: dict[str, dict[str, object]] = {}
    for target, raw_entries in sorted(dependencies.items()):
        if not isinstance(target, str) or not target or type(raw_entries) is not dict:
            raise ValueError(f"NuGet lock target is invalid: {path}")
        normalized_entries: dict[str, object] = {}
        folded_names: set[str] = set()
        for name, raw_entry in sorted(raw_entries.items(), key=lambda item: item[0].casefold()):
            if not isinstance(name, str) or not name or type(raw_entry) is not dict:
                raise ValueError(f"NuGet lock dependency entry is invalid: {path}")
            folded = name.casefold()
            if folded in folded_names:
                raise ValueError(f"NuGet lock dependency name is case-ambiguous: {path}:{name}")
            folded_names.add(folded)
            dependency_type = raw_entry.get("type")
            if dependency_type not in {"Direct", "Transitive", "Project"}:
                raise ValueError(f"NuGet lock dependency type is invalid: {path}:{name}")

            entry: dict[str, object] = {"type": dependency_type}
            requested = raw_entry.get("requested")
            if requested is not None:
                if not isinstance(requested, str) or not requested:
                    raise ValueError(f"NuGet lock requested range is invalid: {path}:{name}")
                entry["requested"] = requested

            resolved = raw_entry.get("resolved")
            content_hash = raw_entry.get("contentHash")
            if dependency_type != "Project":
                if not isinstance(resolved, str) or not resolved or resolved != resolved.strip():
                    raise ValueError(f"NuGet lock resolved version is missing: {path}:{name}")
                if not _valid_sha512_content_hash(content_hash):
                    raise ValueError(f"NuGet lock contentHash is invalid: {path}:{name}")
                entry["resolved"] = resolved
                entry["contentHash"] = content_hash
            elif resolved is not None or content_hash is not None or requested is not None:
                raise ValueError(f"NuGet project lock entry has package bytes/range: {path}:{name}")

            child_dependencies = raw_entry.get("dependencies")
            if child_dependencies is not None:
                if type(child_dependencies) is not dict or not all(
                    isinstance(child_name, str)
                    and child_name
                    and isinstance(child_range, str)
                    and child_range
                    for child_name, child_range in child_dependencies.items()
                ):
                    raise ValueError(f"NuGet lock child dependencies are invalid: {path}:{name}")
                entry["dependencies"] = {
                    child_name: child_dependencies[child_name]
                    for child_name in sorted(child_dependencies, key=str.casefold)
                }

            unexpected = set(raw_entry) - {
                "type",
                "requested",
                "resolved",
                "contentHash",
                "dependencies",
            }
            if unexpected:
                raise ValueError(
                    f"NuGet lock dependency entry has unexpected fields: {path}:{name}: "
                    + ", ".join(sorted(unexpected))
                )
            normalized_entries[name] = entry
        normalized_targets[target] = normalized_entries

    return {"version": 1, "dependencies": normalized_targets}


def _package_references(project: Path) -> dict[str, tuple[str, str, str]]:
    """Return case-folded (name, requested exact range, resolved version) identities."""

    try:
        tree = ET.parse(project)
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"PackageReference project is unreadable: {project}") from error
    refs: dict[str, tuple[str, str, str]] = {}
    for node in tree.findall(".//PackageReference"):
        name = node.attrib.get("Include") or node.attrib.get("Update")
        requested = node.attrib.get("Version")
        if requested is None:
            child = node.find("Version")
            requested = child.text.strip() if child is not None and child.text else None
        if not name or not requested:
            raise ValueError(f"PackageReference must have exact Include/Version: {project}")
        match = EXACT_NUGET_VERSION.fullmatch(requested)
        if match is None:
            raise ValueError(f"PackageReference must use exact NuGet range: {project}:{name}")
        folded = name.casefold()
        if folded in refs:
            raise ValueError(f"PackageReference is duplicate/case-ambiguous: {project}:{name}")
        refs[folded] = (name, requested, match.group(1))
    return refs


def dotnet_lock_content_blockers(root: Path, project: Path) -> list[str]:
    """Require the sibling NuGet lock to bind every declared direct package exactly."""

    relative = project.relative_to(root).as_posix()
    lock_path = project.parent / "packages.lock.json"
    if not lock_path.is_file():
        return [f"DOTNET_PROJECT_LOCK_MISSING:{relative}"]

    try:
        refs = _package_references(project)
    except ValueError as error:
        return [f"DOTNET_PROJECT_PACKAGE_REFERENCE_INVALID:{relative}:{error}"]
    if not refs:
        return []
    try:
        lock = normalized_dotnet_lock(lock_path)
    except ValueError as error:
        return [f"DOTNET_PROJECT_LOCK_CONTENT_INVALID:{relative}:{error}"]

    blockers: list[str] = []
    seen: dict[str, int] = {folded: 0 for folded in refs}
    targets = lock["dependencies"]
    assert isinstance(targets, dict)
    for target_name, target in targets.items():
        assert isinstance(target_name, str) and isinstance(target, dict)
        for package_name, record in target.items():
            assert isinstance(package_name, str) and isinstance(record, dict)
            if record.get("type") != "Direct":
                continue
            folded = package_name.casefold()
            expected = refs.get(folded)
            if expected is None:
                # SDK auto-references may appear as Direct without a csproj declaration.
                continue
            expected_name, expected_requested, expected_resolved = expected
            seen[folded] += 1
            if package_name != expected_name:
                blockers.append(
                    f"DOTNET_PROJECT_LOCK_NAME_CASE_MISMATCH:{relative}:{expected_name}:{package_name}"
                )
            if record.get("requested") != expected_requested:
                blockers.append(
                    f"DOTNET_PROJECT_LOCK_REQUESTED_MISMATCH:{relative}:{expected_name}:"
                    f"{expected_requested}:{record.get('requested')}"
                )
            if record.get("resolved") != expected_resolved:
                blockers.append(
                    f"DOTNET_PROJECT_LOCK_RESOLVED_MISMATCH:{relative}:{expected_name}@"
                    f"{expected_resolved}:{record.get('resolved')}"
                )

    for folded, (name, requested, _resolved) in sorted(refs.items()):
        if seen[folded] == 0:
            blockers.append(f"DOTNET_PROJECT_LOCK_DIRECT_MISSING:{relative}:{name}@{requested}")

    return sorted(set(blockers))
