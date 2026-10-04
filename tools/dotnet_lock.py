from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def _strict_json(text: str):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON object key: {key}')
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicates)


def _package_references(project: Path) -> dict[str, str]:
    """Return canonical direct PackageReference identities for one project.

    This mirrors the repository's existing Include/Update + Version handling but
    additionally rejects duplicate identities and case-insensitive ambiguity.
    """
    tree = ET.parse(project)
    refs: dict[str, tuple[str, str]] = {}
    for node in tree.findall('.//PackageReference'):
        name = node.attrib.get('Include') or node.attrib.get('Update')
        version = node.attrib.get('Version')
        if version is None:
            child = node.find('Version')
            version = child.text.strip() if child is not None and child.text else None
        if not name or not version:
            raise ValueError('PackageReference must have canonical name and exact version')
        folded = name.casefold()
        if folded in refs:
            previous_name, previous_version = refs[folded]
            if previous_name != name or previous_version != version:
                raise ValueError(f'ambiguous PackageReference:{name}')
            raise ValueError(f'duplicate PackageReference:{name}')
        refs[folded] = (name, version)
    return {name: version for name, version in refs.values()}


def _valid_sha512_content_hash(value: object) -> bool:
    """NuGet lock contentHash is the base64-encoded 64-byte SHA-512 value."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == 64


def dotnet_lock_content_blockers(root: Path, project: Path) -> list[str]:
    """Fail closed when a project's NuGet lock does not bind declared packages.

    The repository's existing workflow checks still remain authoritative for
    `--locked-mode`. This function closes the local evidence gap: a file merely
    named packages.lock.json is not evidence that its direct dependency records
    match the project or carry NuGet content hashes.
    """
    relative = project.relative_to(root).as_posix()
    lock_path = project.parent / 'packages.lock.json'
    if not lock_path.is_file():
        return [f'DOTNET_PROJECT_LOCK_MISSING:{relative}']

    try:
        document = _strict_json(lock_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return [f'DOTNET_PROJECT_LOCK_INVALID_JSON:{relative}']
    if not isinstance(document, dict):
        return [f'DOTNET_PROJECT_LOCK_INVALID_ROOT:{relative}']
    if document.get('version') != 1:
        return [f'DOTNET_PROJECT_LOCK_VERSION_UNSUPPORTED:{relative}:{document.get("version")}']
    targets = document.get('dependencies')
    if not isinstance(targets, dict) or not targets:
        return [f'DOTNET_PROJECT_LOCK_DEPENDENCIES_INVALID:{relative}']

    try:
        refs = _package_references(project)
    except (ET.ParseError, ValueError) as error:
        return [f'DOTNET_PROJECT_PACKAGE_REFERENCE_AMBIGUOUS:{relative}:{error}']

    blockers: list[str] = []
    seen_direct: dict[str, list[tuple[str, str, object]]] = {
        name.casefold(): [] for name in refs
    }
    declared_casefold = {name.casefold(): (name, version) for name, version in refs.items()}

    for target_name, target in sorted(targets.items(), key=lambda item: str(item[0])):
        if not isinstance(target_name, str) or not target_name or not isinstance(target, dict):
            blockers.append(f'DOTNET_PROJECT_LOCK_TARGET_INVALID:{relative}')
            continue
        for package_name, record in sorted(target.items(), key=lambda item: str(item[0]).casefold()):
            if not isinstance(package_name, str) or not isinstance(record, dict):
                blockers.append(f'DOTNET_PROJECT_LOCK_RECORD_INVALID:{relative}:{target_name}')
                continue
            if record.get('type') != 'Direct':
                continue
            folded = package_name.casefold()
            if folded not in declared_casefold:
                # SDK auto-referenced packages can be Direct in some project types.
                # Do not claim they came from the csproj, but leave them to locked
                # restore instead of falsely marking them as stale user references.
                continue
            expected_name, expected_version = declared_casefold[folded]
            resolved = record.get('resolved')
            content_hash = record.get('contentHash')
            seen_direct[folded].append((target_name, str(resolved), content_hash))
            if package_name != expected_name:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_NAME_CASE_MISMATCH:{relative}:{expected_name}:{package_name}'
                )
            if resolved != expected_version:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_RESOLVED_MISMATCH:{relative}:{expected_name}@{expected_version}:{resolved}'
                )
            if not _valid_sha512_content_hash(content_hash):
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_CONTENT_HASH_INVALID:{relative}:{expected_name}:{target_name}'
                )

    for folded, (name, version) in sorted(declared_casefold.items()):
        if not seen_direct[folded]:
            blockers.append(
                f'DOTNET_PROJECT_LOCK_DIRECT_MISSING:{relative}:{name}@{version}'
            )

    return blockers


def dotnet_locked_dependency_graph(root: Path, package_projects: list[Path]) -> list[dict[str, str]]:
    """Return a deterministic, target-aware, NuGet-hash-bound package graph.

    This is intended as the replacement source for release-manifest
    `dotnet_package_dependencies` once a .NET package is admitted. Direct-only
    csproj name/version pairs are insufficient release provenance because they
    omit transitive packages and NuGet's content integrity identity.
    """
    graph: list[dict[str, str]] = []
    for project in sorted(set(package_projects)):
        relative = project.relative_to(root).as_posix()
        blockers = dotnet_lock_content_blockers(root, project)
        if blockers:
            raise ValueError(
                f'NuGet lock does not match project {relative}: ' + ';'.join(blockers)
            )
        lock_path = project.parent / 'packages.lock.json'
        try:
            document = _strict_json(lock_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError(f'invalid NuGet lock for {relative}') from error
        if not isinstance(document, dict) or document.get('version') != 1:
            raise ValueError(f'unsupported NuGet lock for {relative}')
        targets = document.get('dependencies')
        if not isinstance(targets, dict) or not targets:
            raise ValueError(f'NuGet lock has no dependency targets for {relative}')
        for target_name, target in sorted(targets.items(), key=lambda item: str(item[0])):
            if not isinstance(target_name, str) or not target_name or not isinstance(target, dict):
                raise ValueError(f'invalid NuGet lock target for {relative}')
            for package_name, record in sorted(target.items(), key=lambda item: str(item[0]).casefold()):
                if not isinstance(package_name, str) or not package_name or not isinstance(record, dict):
                    raise ValueError(f'invalid NuGet lock record for {relative}:{target_name}')
                kind = record.get('type')
                # Project references are source composition, not NuGet package artifacts.
                if kind == 'Project':
                    continue
                if kind not in {'Direct', 'Transitive'}:
                    raise ValueError(
                        f'unsupported NuGet lock dependency type for {relative}:{target_name}:{package_name}'
                    )
                resolved = record.get('resolved')
                content_hash = record.get('contentHash')
                if not isinstance(resolved, str) or not resolved or resolved != resolved.strip():
                    raise ValueError(
                        f'invalid NuGet resolved version for {relative}:{target_name}:{package_name}'
                    )
                if not _valid_sha512_content_hash(content_hash):
                    raise ValueError(
                        f'invalid NuGet content hash for {relative}:{target_name}:{package_name}'
                    )
                item = {
                    'project': relative,
                    'target': target_name,
                    'name': package_name,
                    'type': kind,
                    'version': resolved,
                    'content_hash_sha512_base64': content_hash,
                }
                if kind == 'Direct':
                    requested = record.get('requested')
                    if not isinstance(requested, str) or not requested:
                        raise ValueError(
                            f'missing NuGet requested range for {relative}:{target_name}:{package_name}'
                        )
                    item['requested'] = requested
                graph.append(item)
    return graph
