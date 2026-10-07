from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from pathlib import Path, PurePosixPath
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
_DOTNET_RESTORE_ENVIRONMENT_AUTHORITY = (
    "RestoreForceEvaluate",
    "NuGetLockFilePath",
)


def _nuget_identity_component_is_path_safe(value: object) -> bool:
    """Reject package identity text that can escape a restored package directory."""

    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and ":" not in value
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
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


def _workflow_block_scalar_content_lines(workflow_text: str) -> set[int]:
    """Return physical lines that are YAML block-scalar content."""

    lines = workflow_text.splitlines()
    content_lines: set[int] = set()
    scalar_indent: int | None = None
    scalar_header = re.compile(r":\s*[|>][+-]?\s*(?:#.*)?$")
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if scalar_indent is not None:
            if not stripped:
                content_lines.add(index)
                continue
            if indent > scalar_indent:
                content_lines.add(index)
                continue
            scalar_indent = None
        if stripped and scalar_header.search(stripped):
            scalar_indent = indent
    return content_lines


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
    block_scalar_lines = _workflow_block_scalar_content_lines(workflow_text)
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
        if (
            line_number not in block_scalar_lines
            and command.startswith("run: dotnet restore ")
        ):
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

def dotnet_restore_workflow_environment_authority_lines(
    workflow_text: str,
) -> list[tuple[int, str]]:
    """Find source-controlled workflow text that can replace lock authority.

    These MSBuild/NuGet property names are forbidden anywhere in executable
    workflow YAML, not only as block-style env keys. GitHub Actions can
    establish them through flow mappings, quoted keys or writes to GITHUB_ENV
    before a later restore, so qualification fails closed on every non-comment
    source occurrence.
    """
    if type(workflow_text) is not str:
        raise TypeError("workflow text must be exact str")

    names = {
        name.casefold(): name
        for name in _DOTNET_RESTORE_ENVIRONMENT_AUTHORITY
    }
    authority_text = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(re.escape(name) for name in _DOTNET_RESTORE_ENVIRONMENT_AUTHORITY)
        + r")(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    findings: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for line_number, raw in enumerate(workflow_text.splitlines(), start=1):
        stripped = raw.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        for match in authority_text.finditer(stripped):
            canonical = names[match.group(1).casefold()]
            finding = (line_number, canonical)
            if finding not in seen:
                findings.append(finding)
                seen.add(finding)
    return findings

def dotnet_restore_command_tokens(command: str) -> tuple[str, ...]:
    """Parse one canonical YAML run-line dotnet restore command."""
    if not isinstance(command, str) or not command.startswith('run: dotnet restore '):
        raise ValueError('not a canonical dotnet restore run line')
    payload = command.removeprefix('run: ')
    if _DOTNET_RESTORE_SHELL_CONTROL.search(payload) is not None:
        raise ValueError('dotnet restore command must not contain shell execution control')
    try:
        # Preserve Windows separators as literal backslashes for the
        # canonical repo-relative target check. POSIX shlex would otherwise
        # silently remove them and admit a noncanonical path.
        tokens = tuple(shlex.split(payload.replace("\\", "\\\\"), comments=True))
    except ValueError as error:
        raise ValueError('malformed dotnet restore command') from error
    if len(tokens) < 3 or tokens[:2] != ('dotnet', 'restore'):
        raise ValueError('not a canonical dotnet restore command')
    return tokens


def dotnet_restore_project_target(
    tokens: tuple[str, ...] | list[str],
) -> str | None:
    """Return one canonical repo-relative csproj restore target."""
    arguments = tuple(tokens[2:])
    if '--' in arguments:
        arguments = arguments[:arguments.index('--')]
    if not arguments or not isinstance(arguments[0], str):
        return None
    target = arguments[0]
    if not target or target.startswith('-') or '\\' in target:
        return None
    path = PurePosixPath(target)
    if (
        path.is_absolute()
        or path.suffix != '.csproj'
        or any(part in ('', '.', '..') for part in path.parts)
        or path.as_posix() != target
    ):
        return None
    return target


def dotnet_restore_targets_project(
    tokens: tuple[str, ...] | list[str],
    project: str,
) -> bool:
    """Require the release project as the canonical restore positional target."""
    if not isinstance(project, str) or not project:
        return False
    return dotnet_restore_project_target(tokens) == project


def dotnet_restore_tokens_are_locked(tokens: tuple[str, ...] | list[str]) -> bool:
    """Accept only effective locked-restore authority before any -- sentinel."""
    arguments = tuple(tokens[2:])
    if '--' in arguments:
        arguments = arguments[:arguments.index('--')]

    locked_flag = '--locked-mode' in arguments
    force_evaluate_flag = '--force-evaluate' in arguments
    custom_lock_path_flag = any(
        token == '--lock-file-path' or token.startswith('--lock-file-path=')
        for token in arguments
    )
    property_values: list[str] = []
    force_evaluate_values: list[str] = []
    custom_lock_path_values: list[str] = []
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
        for assignment in re.split(r'[;,]', payload):
            if '=' not in assignment:
                continue
            name, value = assignment.split('=', 1)
            folded_name = name.casefold()
            folded_value = value.casefold()
            if folded_name == 'restorelockedmode':
                property_values.append(folded_value)
            elif folded_name == 'restoreforceevaluate':
                force_evaluate_values.append(folded_value)
            elif folded_name == 'nugetlockfilepath':
                custom_lock_path_values.append(value)

    # This gate validates the committed sibling packages.lock.json. A custom
    # lock-file path would make restore consume different authority.
    if custom_lock_path_flag or custom_lock_path_values:
        return False

    # RestoreForceEvaluate overrides RestoreLockedMode and permits regenerating
    # the package lock graph. Any enabled or non-canonical force-evaluate
    # authority defeats repeatable locked-restore evidence.
    if force_evaluate_flag or any(
        value != 'false' for value in force_evaluate_values
    ):
        return False

    # Any explicit contradictory/non-true locked-mode assignment defeats the
    # assertion, including a later value that could override --locked-mode.
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


_RESTORE_AUTHORITY_PROPERTIES = (
    'RestoreForceEvaluate',
    'NuGetLockFilePath',
)
_RESTORE_GLOBAL_AUTHORITY_PROPERTIES = (
    'RestoreLockedMode',
    'RestoreForceEvaluate',
    'NuGetLockFilePath',
)


def _restore_authority_property_names(tree: ET.ElementTree) -> tuple[str, ...]:
    present = {
        _xml_local_name(node.tag).casefold()
        for node in tree.iter()
    }
    return tuple(
        name
        for name in _RESTORE_AUTHORITY_PROPERTIES
        if name.casefold() in present
    )


def _restore_local_override_names(tree: ET.ElementTree) -> tuple[str, ...]:
    raw = tree.getroot().attrib.get('TreatAsLocalProperty')
    if not isinstance(raw, str) or not raw.strip():
        return ()
    declared = {
        token.strip().casefold()
        for token in raw.split(';')
        if token.strip()
    }
    return tuple(
        name
        for name in _RESTORE_GLOBAL_AUTHORITY_PROPERTIES
        if name.casefold() in declared
    )


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
            project_root = tree.getroot()
            sdk_attribute = project_root.attrib.get('Sdk')
            if sdk_attribute is not None and not (
                sdk_attribute == 'Microsoft.NET.Sdk'
                or (relative == 'src/AutoTrade.Host/AutoTrade.Host.csproj'
                    and sdk_attribute == 'Microsoft.NET.Sdk.Web')
            ):
                blockers.append(
                    f'DOTNET_PROJECT_SDK_AUTHORITY_UNSUPPORTED:'
                    f'{relative}:{sdk_attribute}'
                )
            if _xml_elements(tree, 'Sdk'):
                blockers.append(
                    f'DOTNET_PROJECT_SDK_ELEMENT_UNSUPPORTED:{relative}'
                )
            if _xml_elements(tree, 'Import'):
                blockers.append(
                    f'DOTNET_EXPLICIT_MSBUILD_IMPORT_UNSUPPORTED:{relative}'
                )
            for property_name in _restore_authority_property_names(tree):
                blockers.append(
                    f'DOTNET_RESTORE_AUTHORITY_PROPERTY_UNSUPPORTED:'
                    f'{relative}:{property_name}'
                )
            for property_name in _restore_local_override_names(tree):
                blockers.append(
                    f'DOTNET_RESTORE_AUTHORITY_LOCAL_OVERRIDE_UNSUPPORTED:'
                    f'{relative}:{property_name}'
                )

    for path in sorted(candidates):
        if any(part in {"obj", "bin"} for part in path.relative_to(root).parts):
            # Generated restore artifacts are outputs, not source-controlled authority.
            continue
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
        for property_name in _restore_authority_property_names(tree):
            blockers.append(
                f'DOTNET_RESTORE_AUTHORITY_PROPERTY_UNSUPPORTED:'
                f'{relative}:{property_name}'
            )
        for property_name in _restore_local_override_names(tree):
            blockers.append(
                f'DOTNET_RESTORE_AUTHORITY_LOCAL_OVERRIDE_UNSUPPORTED:'
                f'{relative}:{property_name}'
            )
    return blockers


def _package_references(project: Path) -> dict[str, str]:
    """Return canonical direct PackageReference identities for one project.

    This mirrors the repository's existing Include/Update + Version handling but
    additionally rejects duplicate identities and case-insensitive ambiguity.
    """
    refs: dict[str, tuple[str, str]] = {}
    for name, version in dotnet_project_package_references(project):
        if (
            not _nuget_identity_component_is_path_safe(name)
            or not _nuget_identity_component_is_path_safe(version)
        ):
            raise ValueError(
                'PackageReference must have path-safe canonical name and exact version'
            )
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
            or not _nuget_identity_component_is_path_safe(dependency_name)
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
                or not _nuget_identity_component_is_path_safe(package_name)
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
            if not _nuget_identity_component_is_path_safe(resolved):
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
            'imported MSBuild PackageReference or restore authority is outside '
            'the static release graph: '
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
                if (
                    not isinstance(package_name, str)
                    or not package_name
                    or not _nuget_identity_component_is_path_safe(package_name)
                    or not isinstance(record, dict)
                ):
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
                if not _nuget_identity_component_is_path_safe(resolved):
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
