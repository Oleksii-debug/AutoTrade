from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from pathlib import Path
import xml.etree.ElementTree as ET


_DOTNET_RESTORE_TEXT = re.compile(
    r"\bdotnet[ \t]+restore(?=$|[ \t\r\n;&|<>()\"'\x60])",
    re.IGNORECASE,
)
_DOTNET_RESTORE_MULTILINE_TEXT = re.compile(
    r"\bdotnet(?:[ \t]*\\?[ \t]*\r?\n[ \t]+)+restore"
    r"(?=$|[ \t\r\n;&|<>()\"'\x60])",
    re.IGNORECASE,
)
_DOTNET_RESTORE_SHELL_CONTROL = re.compile(
    r"(?:&&|\|\||[;&|<>\x60]|\$\()"
)


def _strict_json(text: str):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON object key: {key}')
            result[key] = value
        return result

    def reject_non_finite(constant: str):
        raise ValueError(f'non-finite JSON constant: {constant}')

    return json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_non_finite,
    )


def _xml_local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ''
    return tag.rsplit('}', 1)[-1]


def _xml_elements(tree: ET.ElementTree, local_name: str):
    return tuple(
        node for node in tree.iter()
        if _xml_local_name(node.tag) == local_name
    )


def dotnet_restore_workflow_commands(
    workflow_text: str,
) -> tuple[list[str], list[int]]:
    """Find canonical restore commands and reject restore text outside direct run lines.

    Qualification supports only one-line run: dotnet restore commands.
    Other executable-looking restore occurrences are surfaced by source line,
    so YAML block scalars or wrapper shell cannot bypass locked-restore inspection.
    """
    if type(workflow_text) is not str:
        raise TypeError("workflow text must be exact str")

    commands: list[str] = []
    unscoped_lines: list[int] = []
    unscoped_line_set: set[int] = set()
    for line_number, raw in enumerate(workflow_text.splitlines(), start=1):
        command = raw.strip()
        if command.startswith("- "):
            command = command[2:].strip()
        if (
            not command
            or command.startswith("#")
            or _DOTNET_RESTORE_TEXT.search(command) is None
        ):
            continue
        if command.startswith("run: dotnet restore "):
            commands.append(command)
        else:
            unscoped_lines.append(line_number)
            unscoped_line_set.add(line_number)

    # YAML folded scalars and shell continuations can join physical lines into
    # one executable "dotnet restore" command. Scan source text across newline
    # boundaries so an additional non-canonical restore cannot hide beside an
    # otherwise valid locked restore command.
    for match in _DOTNET_RESTORE_MULTILINE_TEXT.finditer(workflow_text):
        line_number = workflow_text.count("\n", 0, match.start()) + 1
        if line_number not in unscoped_line_set:
            unscoped_lines.append(line_number)
            unscoped_line_set.add(line_number)

    unscoped_lines.sort()
    return commands, unscoped_lines

def dotnet_restore_command_tokens(command: str) -> tuple[str, ...]:
    """Parse one canonical YAML run-line dotnet restore command."""
    if not isinstance(command, str) or not command.startswith('run: dotnet restore '):
        raise ValueError('not a canonical dotnet restore run line')
    payload = command.removeprefix('run: ')
    if _DOTNET_RESTORE_SHELL_CONTROL.search(payload) is not None:
        raise ValueError('dotnet restore command must not contain shell execution control')
    try:
        tokens = tuple(shlex.split(payload, comments=True))
    except ValueError as error:
        raise ValueError('malformed dotnet restore command') from error
    if len(tokens) < 3 or tokens[:2] != ('dotnet', 'restore'):
        raise ValueError('not a canonical dotnet restore command')
    return tokens


def dotnet_restore_targets_project(
    tokens: tuple[str, ...] | list[str],
    project: str,
) -> bool:
    """Require the release project as the canonical restore positional target."""
    if not isinstance(project, str) or not project or project.startswith('-'):
        return False
    arguments = tuple(tokens[2:])
    if '--' in arguments:
        arguments = arguments[:arguments.index('--')]
    return bool(arguments) and arguments[0] == project


def dotnet_restore_tokens_are_locked(tokens: tuple[str, ...] | list[str]) -> bool:
    """Accept only effective locked-restore authority before any -- sentinel."""
    arguments = tuple(tokens[2:])
    if '--' in arguments:
        arguments = arguments[:arguments.index('--')]

    locked_flag = '--locked-mode' in arguments
    property_values: list[str] = []
    prefixes = ('-p:', '/p:', '-property:', '/property:')
    for token in arguments:
        lowered = token.casefold()
        prefix = next(
            (candidate for candidate in prefixes if lowered.startswith(candidate)),
            None,
        )
        if prefix is None:
            continue
        payload = token[len(prefix):]
        for assignment in payload.split(';'):
            if '=' not in assignment:
                continue
            name, value = assignment.split('=', 1)
            if name.casefold() == 'restorelockedmode':
                property_values.append(value.casefold())

    # Any explicit contradictory/non-true assignment defeats the assertion,
    # including a later value that could override --locked-mode.
    if property_values and any(value != 'true' for value in property_values):
        return False
    return locked_flag or bool(property_values)


def dotnet_project_package_references(project: Path) -> list[tuple[str | None, str | None]]:
    """Read effective-in-file PackageReference declarations namespace-agnostically."""
    tree = ET.parse(project)
    references: list[tuple[str | None, str | None]] = []
    for node in _xml_elements(tree, 'PackageReference'):
        name = node.attrib.get('Include') or node.attrib.get('Update')
        version = node.attrib.get('Version')
        if version is None:
            child = next(
                (item for item in node if _xml_local_name(item.tag) == 'Version'),
                None,
            )
            version = (
                child.text.strip()
                if child is not None and child.text
                else None
            )
        references.append((name, version))
    return references


def dotnet_imported_package_reference_blockers(root: Path) -> list[str]:
    """Reject release dependency declarations hidden in imported MSBuild files.

    Static release provenance currently binds PackageReference declarations that
    live in src/*.csproj.  A PackageReference injected by a root/source .props or
    .targets file would otherwise bypass project discovery and the lock gate.
    Fail closed until evaluated MSBuild dependency discovery is authoritative.
    """
    candidates: set[Path] = set()
    for pattern in ('*.props', '*.targets'):
        candidates.update(root.glob(pattern))
        source_root = root / 'src'
        if source_root.is_dir():
            candidates.update(source_root.rglob(pattern))

    blockers: list[str] = []

    # The static release graph does not evaluate arbitrary explicit MSBuild
    # imports. An imported file can inject PackageReference items from outside
    # the root/src .props/.targets scan, so any explicit project Import is a
    # dependency-authority boundary until evaluated MSBuild discovery exists.
    source_root = root / 'src'
    if source_root.is_dir():
        for project in sorted(source_root.rglob('*.csproj')):
            relative = project.relative_to(root).as_posix()
            try:
                tree = ET.parse(project)
            except (OSError, ET.ParseError):
                blockers.append(f'DOTNET_MSBUILD_PROJECT_INVALID:{relative}')
                continue
            if _xml_elements(tree, 'Import'):
                blockers.append(
                    f'DOTNET_EXPLICIT_MSBUILD_IMPORT_UNSUPPORTED:{relative}'
                )

    for path in sorted(candidates):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ET.parse(path)
        except (OSError, ET.ParseError):
            blockers.append(f'DOTNET_MSBUILD_DEPENDENCY_SOURCE_INVALID:{relative}')
            continue
        if _xml_elements(tree, 'Import'):
            blockers.append(
                f'DOTNET_EXPLICIT_MSBUILD_IMPORT_UNSUPPORTED:{relative}'
            )
        if _xml_elements(tree, 'PackageReference'):
            blockers.append(
                f'DOTNET_IMPORTED_PACKAGE_REFERENCE_UNSUPPORTED:{relative}'
            )
    return blockers


def _package_references(project: Path) -> dict[str, str]:
    """Return canonical direct PackageReference identities for one project.

    This mirrors the repository's existing Include/Update + Version handling but
    additionally rejects duplicate identities and case-insensitive ambiguity.
    """
    refs: dict[str, tuple[str, str]] = {}
    for name, version in dotnet_project_package_references(project):
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
    return (
        len(decoded) == 64
        and base64.b64encode(decoded).decode('ascii') == value
    )


def _lock_dependency_edges(
    value: object,
    *,
    relative: str,
    target_name: str,
    package_name: str,
) -> list[dict[str, str]]:
    """Return canonical dependency-edge identities for one NuGet lock record."""
    if value is None:
        return []
    if not isinstance(value, dict):
        raise ValueError(
            f'invalid NuGet dependency edges for {relative}:{target_name}:{package_name}'
        )

    edges: list[dict[str, str]] = []
    seen_names: dict[str, str] = {}
    for dependency_name, requested in sorted(
        value.items(), key=lambda item: str(item[0]).casefold()
    ):
        if (
            not isinstance(dependency_name, str)
            or not dependency_name
            or dependency_name != dependency_name.strip()
        ):
            raise ValueError(
                f'invalid NuGet dependency edge name for '
                f'{relative}:{target_name}:{package_name}'
            )
        folded = dependency_name.casefold()
        previous = seen_names.get(folded)
        if previous is not None:
            raise ValueError(
                f'ambiguous NuGet dependency edge package id for '
                f'{relative}:{target_name}:{package_name}:{previous}:{dependency_name}'
            )
        seen_names[folded] = dependency_name
        if (
            not isinstance(requested, str)
            or not requested
            or requested != requested.strip()
        ):
            raise ValueError(
                f'invalid NuGet dependency edge requirement for '
                f'{relative}:{target_name}:{package_name}:{dependency_name}'
            )
        edges.append({'name': dependency_name, 'requested': requested})
    return edges


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
    lock_version = document.get('version')
    if type(lock_version) is not int or lock_version != 1:
        return [f'DOTNET_PROJECT_LOCK_VERSION_UNSUPPORTED:{relative}:{lock_version}']
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
        seen_target_packages: dict[str, str] = {}
        target_dependency_edges: list[tuple[str, list[dict[str, str]]]] = []
        for package_name, record in sorted(target.items(), key=lambda item: str(item[0]).casefold()):
            if (
                not isinstance(package_name, str)
                or not package_name
                or package_name != package_name.strip()
                or not isinstance(record, dict)
            ):
                blockers.append(f'DOTNET_PROJECT_LOCK_RECORD_INVALID:{relative}:{target_name}')
                continue
            folded = package_name.casefold()
            previous_name = seen_target_packages.get(folded)
            if previous_name is not None:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_PACKAGE_CASE_AMBIGUOUS:'
                    f'{relative}:{target_name}:{previous_name}:{package_name}'
                )
                continue
            seen_target_packages[folded] = package_name

            kind = record.get('type')
            if kind == 'Project':
                continue
            if kind not in {'Direct', 'Transitive'}:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_DEPENDENCY_TYPE_UNSUPPORTED:'
                    f'{relative}:{target_name}:{package_name}:{kind}'
                )
                continue

            resolved = record.get('resolved')
            content_hash = record.get('contentHash')
            if (
                not isinstance(resolved, str)
                or not resolved
                or resolved != resolved.strip()
            ):
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_RESOLVED_INVALID:'
                    f'{relative}:{target_name}:{package_name}'
                )
            if not _valid_sha512_content_hash(content_hash):
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_CONTENT_HASH_INVALID:'
                    f'{relative}:{package_name}:{target_name}'
                )
            try:
                dependency_edges = _lock_dependency_edges(
                    record.get('dependencies'),
                    relative=relative,
                    target_name=target_name,
                    package_name=package_name,
                )
            except ValueError:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_DEPENDENCY_EDGES_INVALID:'
                    f'{relative}:{target_name}:{package_name}'
                )
                dependency_edges = []
            target_dependency_edges.append((package_name, dependency_edges))

            if kind != 'Direct':
                continue
            requested = record.get('requested')
            if (
                not isinstance(requested, str)
                or not requested
                or requested != requested.strip()
            ):
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_REQUESTED_INVALID:'
                    f'{relative}:{target_name}:{package_name}'
                )
            if folded not in declared_casefold:
                # SDK auto-referenced packages can be Direct in some project types.
                # They remain valid NuGet artifacts and therefore still require
                # canonical resolved/hash/requested/dependency-edge evidence.
                continue
            expected_name, expected_version = declared_casefold[folded]
            seen_direct[folded].append((target_name, str(resolved), content_hash))
            if package_name != expected_name:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_NAME_CASE_MISMATCH:{relative}:{expected_name}:{package_name}'
                )
            if resolved != expected_version:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_RESOLVED_MISMATCH:{relative}:{expected_name}@{expected_version}:{resolved}'
                )
            expected_requested = f'[{expected_version}, )'
            if isinstance(requested, str) and requested != expected_requested:
                blockers.append(
                    f'DOTNET_PROJECT_LOCK_REQUESTED_MISMATCH:'
                    f'{relative}:{expected_name}@{expected_version}:{requested}'
                )

        for package_name, dependency_edges in target_dependency_edges:
            for edge in dependency_edges:
                dependency_name = edge['name']
                target_record_name = seen_target_packages.get(dependency_name.casefold())
                if target_record_name is None:
                    blockers.append(
                        f'DOTNET_PROJECT_LOCK_DEPENDENCY_TARGET_MISSING:'
                        f'{relative}:{target_name}:{package_name}:{dependency_name}'
                    )
                elif dependency_name != target_record_name:
                    blockers.append(
                        f'DOTNET_PROJECT_LOCK_DEPENDENCY_EDGE_CASE_MISMATCH:'
                        f'{relative}:{target_name}:{package_name}:'
                        f'{dependency_name}:{target_record_name}'
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
    imported_blockers = dotnet_imported_package_reference_blockers(root)
    if imported_blockers:
        raise ValueError(
            'imported MSBuild PackageReference is outside the static release graph: '
            + ';'.join(imported_blockers)
        )

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
        if (
            not isinstance(document, dict)
            or type(document.get('version')) is not int
            or document.get('version') != 1
        ):
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
                    'dependencies': _lock_dependency_edges(
                        record.get('dependencies'),
                        relative=relative,
                        target_name=target_name,
                        package_name=package_name,
                    ),
                }
                if kind == 'Direct':
                    requested = record.get('requested')
                    if (
                        not isinstance(requested, str)
                        or not requested
                        or requested != requested.strip()
                    ):
                        raise ValueError(
                            f'missing NuGet requested range for {relative}:{target_name}:{package_name}'
                        )
                    item['requested'] = requested
                graph.append(item)
    return graph
