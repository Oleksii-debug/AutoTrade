from __future__ import annotations

import argparse
import base64
import binascii
from hashlib import sha512
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import zipfile
import xml.etree.ElementTree as ET

if __package__:
    from .dotnet_lock import (
        dotnet_locked_dependency_graph,
        dotnet_project_package_references,
    )
else:
    from dotnet_lock import (
        dotnet_locked_dependency_graph,
        dotnet_project_package_references,
    )


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "provenance" / "dotnet-package-rights.json"
_SCHEMA_VERSION = "1.0.0"
_VERIFY_RESTORED_PREFIX = (
    "run: python tools/dotnet_package_rights.py --verify-restored "
)
_VERIFY_SHELL_CONTROL = re.compile(r"(?:&&|\|\||[;&|<>`]|[$][(])")
_CANONICAL_NUGET_PACKAGES_AUTHORITY = (
    "NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages"
)
_CANONICAL_VERIFY_PACKAGES_ROOT = "${{ env.NUGET_PACKAGES }}"
_CANONICAL_WORKFLOW_STEP_INDENT = 6
_CONCRETE_PATH_TYPE = type(Path())

_REQUIRED_PACKAGE_FIELDS = frozenset(
    {
        "name",
        "version",
        "content_hash_sha512_base64",
        "license_id",
        "license_file",
        "expected_license_text_path",
        "notice_file",
    }
)


def _strict_json(text: str):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    def reject_non_finite(value: str):
        raise ValueError(f"non-finite JSON constant: {value}")

    return json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_non_finite,
    )


def _valid_content_hash(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return (
        len(decoded) == 64
        and base64.b64encode(decoded).decode("ascii") == value
    )


def _canonical_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be canonical non-empty text")
    if any(character in value for character in ("\x00", "\r", "\n", "\t")):
        raise ValueError(f"{field} contains a control character")
    return value


def _package_projects(root: Path) -> list[Path]:
    projects: list[Path] = []
    for project in sorted((root / "src").rglob("*.csproj")):
        if dotnet_project_package_references(project):
            projects.append(project)
    return projects


def _artifact_key(item: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(item["name"]).casefold(),
        str(item["version"]),
        str(item["content_hash_sha512_base64"]),
    )


def locked_package_artifacts(
    root: Path = ROOT,
    *,
    projects: list[Path] | None = None,
) -> list[dict[str, str]]:
    if projects is None:
        selected = _package_projects(root)
    else:
        selected = [
            project
            for project in sorted(set(projects))
            if dotnet_project_package_references(project)
        ]
    graph = dotnet_locked_dependency_graph(root, selected)
    artifacts: dict[tuple[str, str, str], dict[str, str]] = {}
    by_name_version: dict[tuple[str, str], str] = {}
    for item in graph:
        name = _canonical_text(item.get("name"), field="package name")
        version = _canonical_text(item.get("version"), field="package version")
        content_hash = item.get("content_hash_sha512_base64")
        if not _valid_content_hash(content_hash):
            raise ValueError(f"invalid content hash for {name}@{version}")
        content_hash = str(content_hash)
        name_version = (name.casefold(), version)
        previous = by_name_version.get(name_version)
        if previous is not None and previous != content_hash:
            raise ValueError(
                f"one package identity has multiple content hashes: {name}@{version}"
            )
        by_name_version[name_version] = content_hash
        artifacts[(name.casefold(), version, content_hash)] = {
            "name": name,
            "version": version,
            "content_hash_sha512_base64": content_hash,
        }
    return sorted(
        artifacts.values(),
        key=lambda item: (
            item["name"].casefold(),
            item["version"],
            item["content_hash_sha512_base64"],
        ),
    )


def _repository_license_path(root: Path, raw: object) -> Path:
    text = _canonical_text(raw, field="expected_license_text_path")
    if "\\" in text:
        raise ValueError("expected_license_text_path must use repository separators")
    relative = PurePosixPath(text)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0:2] != ("provenance", "licenses")
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(
            "expected_license_text_path must be beneath provenance/licenses"
        )
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("expected license text cannot traverse a symlink")
    resolved = current.resolve(strict=True)
    provenance = (root / "provenance" / "licenses").resolve(strict=True)
    if not resolved.is_file() or not resolved.is_relative_to(provenance):
        raise ValueError("expected license text is outside provenance/licenses")
    return resolved


def package_rights_records(root: Path = ROOT) -> list[dict[str, str]]:
    try:
        document = _strict_json(
            (root / "provenance" / "dotnet-package-rights.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("NuGet package-rights policy is unreadable") from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "packages"}:
        raise ValueError("NuGet package-rights policy root fields mismatch")
    if document.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("unsupported NuGet package-rights policy version")
    raw_packages = document.get("packages")
    if not isinstance(raw_packages, list):
        raise ValueError("NuGet package-rights packages must be a list")

    records: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(raw_packages):
        if not isinstance(raw, dict) or frozenset(raw) != _REQUIRED_PACKAGE_FIELDS:
            raise ValueError(f"NuGet package-rights record fields mismatch at {index}")
        name = _canonical_text(raw["name"], field="name")
        version = _canonical_text(raw["version"], field="version")
        content_hash = raw["content_hash_sha512_base64"]
        if not _valid_content_hash(content_hash):
            raise ValueError(f"invalid package-rights content hash for {name}@{version}")
        license_id = _canonical_text(raw["license_id"], field="license_id")
        license_file = _canonical_text(raw["license_file"], field="license_file")
        notice_file = _canonical_text(raw["notice_file"], field="notice_file")
        if PurePosixPath(license_file).name != license_file:
            raise ValueError("license_file must be one package-root filename")
        if PurePosixPath(notice_file).name != notice_file:
            raise ValueError("notice_file must be one package-root filename")
        expected_path = _repository_license_path(
            root,
            raw["expected_license_text_path"],
        )
        if not expected_path.read_bytes():
            raise ValueError(f"expected license text is empty for {name}@{version}")
        record = {
            "name": name,
            "version": version,
            "content_hash_sha512_base64": str(content_hash),
            "license_id": license_id,
            "license_file": license_file,
            "expected_license_text_path": expected_path.relative_to(root.resolve(strict=True)).as_posix(),
            "notice_file": notice_file,
        }
        key = _artifact_key(record)
        if key in seen:
            raise ValueError(f"duplicate NuGet package-rights record: {name}@{version}")
        seen.add(key)
        records.append(record)
    return sorted(records, key=_artifact_key)


def _direct_workflow_run_command(raw: str) -> str | None:
    """Return one canonical top-level step command, never block-scalar text."""

    if type(raw) is not str:
        raise TypeError("workflow line must be exact str")
    indent = len(raw) - len(raw.lstrip(" "))
    stripped = raw.strip()
    prefix = "- run: "
    if (
        indent != _CANONICAL_WORKFLOW_STEP_INDENT
        or not stripped.startswith(prefix)
    ):
        return None
    command = stripped.removeprefix(prefix)
    return command if command else None


def _workflow_step_has_bypass(lines: list[str], command_index: int) -> bool:
    """Reject verifier steps that can be skipped or can suppress exit semantics."""

    raw = lines[command_index]
    command_indent = len(raw) - len(raw.lstrip(" "))
    stripped = raw.strip()
    step_start = command_index
    step_indent: int | None = None
    if stripped.startswith("- "):
        step_indent = command_indent
    else:
        for index in range(command_index - 1, -1, -1):
            candidate = lines[index]
            if not candidate.strip():
                continue
            indent = len(candidate) - len(candidate.lstrip(" "))
            if indent >= command_indent:
                continue
            if candidate.lstrip(" ").startswith("- "):
                step_start = index
                step_indent = indent
                break
            if indent < command_indent - 2:
                break
    if step_indent is None:
        return True

    step_end = len(lines)
    for index in range(step_start + 1, len(lines)):
        candidate = lines[index]
        if not candidate.strip():
            continue
        indent = len(candidate) - len(candidate.lstrip(" "))
        if indent < step_indent:
            step_end = index
            break
        if indent == step_indent and candidate.lstrip(" ").startswith("- "):
            step_end = index
            break

    forbidden = ("if:", "continue-on-error:", "shell:")
    return any(
        lines[index].strip().startswith(forbidden)
        for index in range(step_start, step_end)
    )


def workflow_restored_rights_projects(
    workflow_text: str,
) -> tuple[list[str], list[int]]:
    """Return exact projects whose restored package bytes are rights-verified."""

    if type(workflow_text) is not str:
        raise TypeError("workflow text must be exact str")

    lines = workflow_text.splitlines()
    projects: list[str] = []
    invalid_lines: list[int] = []
    for command_index, raw in enumerate(lines):
        line_number = command_index + 1
        direct = _direct_workflow_run_command(raw)
        candidate = raw.strip()
        if (
            "tools/dotnet_package_rights.py" not in candidate
            or "--verify-restored" not in candidate
        ):
            continue
        if (
            direct is None
            and candidate.startswith("- run: ")
            and _workflow_job_name(lines, line_number) is None
        ):
            # Preserve the project identity for the later same-job/order fence,
            # but never treat an out-of-jobs verifier as executable authority.
            direct = candidate.removeprefix("- run: ")
        if direct is None:
            invalid_lines.append(line_number)
            continue
        command = "run: " + direct
        if not command.startswith(_VERIFY_RESTORED_PREFIX):
            invalid_lines.append(line_number)
            continue
        if _workflow_step_has_bypass(lines, command_index):
            invalid_lines.append(line_number)
            continue
        payload = direct
        if _VERIFY_SHELL_CONTROL.search(payload) is not None:
            invalid_lines.append(line_number)
            continue
        try:
            tokens = tuple(shlex.split(payload, comments=True))
        except ValueError:
            invalid_lines.append(line_number)
            continue
        if (
            len(tokens) != 7
            or tokens[:4]
            != (
                "python",
                "tools/dotnet_package_rights.py",
                "--verify-restored",
                "--packages-root",
            )
            or tokens[4] != _CANONICAL_VERIFY_PACKAGES_ROOT
            or tokens[5] != "--project"
        ):
            invalid_lines.append(line_number)
            continue
        project = tokens[6]
        path = PurePosixPath(project)
        if (
            not project
            or "\\" in project
            or path.is_absolute()
            or path.suffix != ".csproj"
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != project
        ):
            invalid_lines.append(line_number)
            continue
        projects.append(project)
    return projects, invalid_lines


def _workflow_job_name(lines: list[str], line_number: int) -> str | None:
    """Return the enclosing top-level GitHub Actions job for one 1-based line."""

    if type(line_number) is not int or not 1 <= line_number <= len(lines):
        raise ValueError("workflow line_number is outside the document")
    candidate: str | None = None
    for index in range(line_number - 1, -1, -1):
        raw = lines[index]
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0:
            return candidate if stripped == "jobs:" else None
        if (
            candidate is None
            and indent == 2
            and stripped.endswith(":")
            and not stripped.startswith("- ")
        ):
            candidate = stripped[:-1]
    return None


def _workflow_rights_blockers(
    root: Path,
    package_projects: list[Path],
) -> list[str]:
    if not package_projects:
        return []

    workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
    try:
        workflow_text = workflow.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ["DOTNET_PACKAGE_RIGHTS_WORKFLOW_MISSING"]

    workflow_lines = workflow_text.splitlines()
    authority_lines = [
        line_number
        for line_number, raw in enumerate(workflow_lines, start=1)
        if raw.strip().startswith("NUGET_PACKAGES:")
    ]
    canonical_authority_lines = [
        line_number
        for line_number, raw in enumerate(workflow_lines, start=1)
        if raw.strip() == _CANONICAL_NUGET_PACKAGES_AUTHORITY
    ]
    unexpected_nuget_authority_lines = [
        line_number
        for line_number, raw in enumerate(workflow_lines, start=1)
        if (
            "NUGET_PACKAGES" in raw
            and raw.strip() != _CANONICAL_NUGET_PACKAGES_AUTHORITY
            and not (
                "tools/dotnet_package_rights.py" in raw
                and _CANONICAL_VERIFY_PACKAGES_ROOT in raw
            )
        )
    ]
    blockers: list[str] = []
    if (
        len(authority_lines) != 1
        or authority_lines != canonical_authority_lines
        or unexpected_nuget_authority_lines
    ):
        blockers.append("DOTNET_PACKAGE_RIGHTS_NUGET_PACKAGES_AUTHORITY_INVALID")

    verified, invalid_lines = workflow_restored_rights_projects(workflow_text)
    for line_number in invalid_lines:
        blockers.append(
            f"DOTNET_PACKAGE_RIGHTS_VERIFY_COMMAND_INVALID:{line_number}"
        )
    seen: set[str] = set()
    for project in verified:
        if project in seen:
            blockers.append(
                f"DOTNET_PACKAGE_RIGHTS_VERIFY_PROJECT_DUPLICATE:{project}"
            )
        seen.add(project)
        if not (root / project).is_file():
            blockers.append(
                f"DOTNET_PACKAGE_RIGHTS_VERIFY_PROJECT_NOT_FOUND:{project}"
            )

    for project in sorted(set(package_projects)):
        relative = project.relative_to(root).as_posix()
        if relative not in seen:
            blockers.append(
                f"DOTNET_PACKAGE_RIGHTS_VERIFY_PROJECT_MISSING:{relative}"
            )
            continue

        restore_command = f"dotnet restore {relative} --locked-mode"
        verify_suffix = f"--project {relative}"
        restore_lines = [
            index
            for index, raw in enumerate(workflow_lines, start=1)
            if _direct_workflow_run_command(raw) == restore_command
        ]
        verify_lines = [
            index
            for index, raw in enumerate(workflow_lines, start=1)
            if (
                (command := _direct_workflow_run_command(raw)) is not None
                and command.startswith(
                    "python tools/dotnet_package_rights.py --verify-restored "
                )
                and command.endswith(verify_suffix)
            )
        ]
        if len(restore_lines) != 1 or len(verify_lines) != 1:
            blockers.append(
                f"DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:{relative}"
            )
            continue
        restore_job = _workflow_job_name(workflow_lines, restore_lines[0])
        verify_job = _workflow_job_name(workflow_lines, verify_lines[0])
        if (
            restore_job is None
            or verify_job is None
            or restore_job != verify_job
            or restore_lines[0] >= verify_lines[0]
        ):
            blockers.append(
                f"DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:{relative}"
            )
            continue
        later_restore = any(
            index > verify_lines[0]
            and _workflow_job_name(workflow_lines, index) == verify_job
            and _direct_workflow_run_command(raw) == restore_command
            for index, raw in enumerate(workflow_lines, start=1)
        )
        if later_restore:
            blockers.append(
                f"DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:{relative}"
            )
    return sorted(blockers)


def package_rights_blockers(root: Path = ROOT) -> list[str]:
    try:
        artifacts = locked_package_artifacts(root)
    except ValueError as error:
        return [f"DOTNET_PACKAGE_RIGHTS_LOCK_INVALID:{error}"]
    try:
        records = package_rights_records(root)
    except ValueError as error:
        return [f"DOTNET_PACKAGE_RIGHTS_POLICY_INVALID:{error}"]

    artifact_keys = {_artifact_key(item): item for item in artifacts}
    policy_keys = {_artifact_key(item): item for item in records}
    blockers: list[str] = []
    for key, artifact in artifact_keys.items():
        if key not in policy_keys:
            blockers.append(
                "DOTNET_PACKAGE_RIGHTS_MISSING:"
                f"{artifact['name']}@{artifact['version']}"
            )
    for key, record in policy_keys.items():
        if key not in artifact_keys:
            blockers.append(
                "DOTNET_PACKAGE_RIGHTS_ORPHANED:"
                f"{record['name']}@{record['version']}"
            )
    blockers.extend(_workflow_rights_blockers(root, _package_projects(root)))
    return sorted(blockers)


def _normalized_license_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"license text is not UTF-8: {path}") from error
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip() + "\n"


def _package_regular_file(
    package_dir: Path,
    candidate: Path,
    *,
    label: str,
) -> Path:
    """Require one non-symlink regular file inside the exact restored package dir."""

    if candidate.is_symlink():
        raise ValueError(f"restored NuGet package {label} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        package_resolved = package_dir.resolve(strict=True)
    except OSError as error:
        raise ValueError(
            f"restored NuGet package {label} is unavailable"
        ) from error
    if not resolved.is_file() or resolved.parent != package_resolved:
        raise ValueError(
            f"restored NuGet package {label} is outside the exact package directory"
        )
    return resolved


def _locked_nupkg_root_evidence(
    nupkg_path: Path,
    *,
    license_file: str,
    notice_file: str,
) -> tuple[bytes, bytes, bytes]:
    """Read exact root rights metadata from the already hash-verified nupkg.

    NuGet's extracted global-packages directory is mutable state. The lock hash
    authenticates the .nupkg payload, so the extracted LICENSE, NOTICE and
    nuspec must be byte-identical to members in that exact archive before they
    are allowed to support a rights decision.
    """

    if type(nupkg_path) is not _CONCRETE_PATH_TYPE:
        raise TypeError("nupkg_path must be exact Path")
    for value, label in (
        (license_file, "license_file"),
        (notice_file, "notice_file"),
    ):
        _canonical_text(value, field=label)
        if PurePosixPath(value).name != value:
            raise ValueError(f"{label} must be one package-root filename")

    try:
        with zipfile.ZipFile(nupkg_path) as archive:
            root_entries: dict[str, tuple[str, zipfile.ZipInfo]] = {}
            for entry in archive.infolist():
                name = entry.filename
                if type(name) is not str or entry.is_dir() or "\\" in name:
                    continue
                relative = PurePosixPath(name)
                if relative.is_absolute() or len(relative.parts) != 1:
                    continue
                key = name.casefold()
                if key in root_entries:
                    raise ValueError(
                        "locked NuGet package has ambiguous root filenames"
                    )
                root_entries[key] = (name, entry)

            def exact_member(name: str, label: str) -> bytes:
                selected = root_entries.get(name.casefold())
                if selected is None or selected[0] != name:
                    raise ValueError(
                        f"locked NuGet package lacks exact root {label}: {name}"
                    )
                return archive.read(selected[1])

            license_bytes = exact_member(license_file, "license")
            notice_bytes = exact_member(notice_file, "notice")
            nuspecs = [
                selected
                for selected in root_entries.values()
                if selected[0].casefold().endswith(".nuspec")
            ]
            if len(nuspecs) != 1:
                raise ValueError(
                    "locked NuGet package must contain one root nuspec"
                )
            nuspec_bytes = archive.read(nuspecs[0][1])
    except ValueError:
        raise
    except (
        zipfile.BadZipFile,
        KeyError,
        RuntimeError,
        OSError,
        NotImplementedError,
    ) as error:
        raise ValueError(
            "locked NuGet package archive is unreadable"
        ) from error

    if not license_bytes:
        raise ValueError("locked NuGet package license is empty")
    if not notice_bytes:
        raise ValueError("locked NuGet package notice is empty")
    if not nuspec_bytes:
        raise ValueError("locked NuGet package nuspec is empty")
    return license_bytes, notice_bytes, nuspec_bytes


def verify_restored_package_rights(
    packages_root: Path,
    *,
    root: Path = ROOT,
    projects: list[Path] | None = None,
) -> None:
    artifacts = locked_package_artifacts(root, projects=projects)
    if not artifacts:
        return
    if not packages_root.is_dir():
        raise ValueError("NuGet global-packages root is unavailable")
    records = {_artifact_key(item): item for item in package_rights_records(root)}
    for artifact in artifacts:
        key = _artifact_key(artifact)
        record = records.get(key)
        if record is None:
            raise ValueError(
                f"NuGet package rights are missing for {artifact['name']}@{artifact['version']}"
            )
        package_name = artifact["name"]
        package_version = artifact["version"]
        for value, label in (
            (package_name, "name"),
            (package_version, "version"),
        ):
            if (
                value in {".", ".."}
                or "/" in value
                or "\\" in value
                or ":" in value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError(
                    f"restored NuGet package {label} is not path-safe: "
                    f"{package_name}@{package_version}"
                )

        packages_root_resolved = packages_root.resolve(strict=True)
        name_dir = packages_root / package_name.casefold()
        package_dir = name_dir / package_version.casefold()
        if name_dir.is_symlink() or package_dir.is_symlink():
            raise ValueError(
                f"restored NuGet package path must not traverse a symlink: "
                f"{package_name}@{package_version}"
            )
        if not package_dir.is_dir():
            raise ValueError(
                f"restored NuGet package is unavailable: {package_name}@{package_version}"
            )
        package_dir_resolved = package_dir.resolve(strict=True)
        if not package_dir_resolved.is_relative_to(packages_root_resolved):
            raise ValueError(
                f"restored NuGet package path escapes packages root: "
                f"{package_name}@{package_version}"
            )

        sha_files = tuple(package_dir.glob("*.nupkg.sha512"))
        nupkg_files = tuple(package_dir.glob("*.nupkg"))
        if len(sha_files) != 1:
            raise ValueError(
                f"restored NuGet package lacks one SHA-512 authority: {package_name}@{package_version}"
            )
        if len(nupkg_files) != 1:
            raise ValueError(
                f"restored NuGet package lacks one nupkg payload: {package_name}@{package_version}"
            )
        sha_path = _package_regular_file(
            package_dir,
            sha_files[0],
            label="SHA-512 authority",
        )
        nupkg_path = _package_regular_file(
            package_dir,
            nupkg_files[0],
            label="nupkg payload",
        )
        restored_hash = sha_path.read_text(encoding="ascii").strip()
        if restored_hash != artifact["content_hash_sha512_base64"]:
            raise ValueError(
                f"restored NuGet package content hash mismatch: {package_name}@{package_version}"
            )
        actual_nupkg_hash = base64.b64encode(
            sha512(nupkg_path.read_bytes()).digest()
        ).decode("ascii")
        if actual_nupkg_hash != artifact["content_hash_sha512_base64"]:
            raise ValueError(
                f"restored NuGet package payload hash mismatch: {package_name}@{package_version}"
            )
        (
            locked_license_bytes,
            locked_notice_bytes,
            locked_nuspec_bytes,
        ) = _locked_nupkg_root_evidence(
            nupkg_path,
            license_file=record["license_file"],
            notice_file=record["notice_file"],
        )
        license_candidate = package_dir / record["license_file"]
        notice_candidate = package_dir / record["notice_file"]
        if not license_candidate.is_file():
            raise ValueError(
                f"restored NuGet package license is missing: {artifact['name']}@{artifact['version']}"
            )
        if not notice_candidate.is_file():
            raise ValueError(
                f"restored NuGet package notice is missing: {artifact['name']}@{artifact['version']}"
            )
        license_path = _package_regular_file(
            package_dir,
            license_candidate,
            label="license",
        )
        notice_path = _package_regular_file(
            package_dir,
            notice_candidate,
            label="notice",
        )
        extracted_license_bytes = license_path.read_bytes()
        extracted_notice_bytes = notice_path.read_bytes()
        if not extracted_notice_bytes:
            raise ValueError(
                f"restored NuGet package notice is empty: {artifact['name']}@{artifact['version']}"
            )
        if extracted_license_bytes != locked_license_bytes:
            raise ValueError(
                f"restored NuGet package license differs from locked nupkg payload: "
                f"{artifact['name']}@{artifact['version']}"
            )
        if extracted_notice_bytes != locked_notice_bytes:
            raise ValueError(
                f"restored NuGet package notice differs from locked nupkg payload: "
                f"{artifact['name']}@{artifact['version']}"
            )
        expected = _normalized_license_text(root / record["expected_license_text_path"])
        actual = _normalized_license_text(license_path)
        if actual != expected:
            raise ValueError(
                f"restored NuGet package license differs from reviewed text: {artifact['name']}@{artifact['version']}"
            )
        nuspecs = tuple(package_dir.glob("*.nuspec"))
        if len(nuspecs) != 1:
            raise ValueError(
                f"restored NuGet package lacks one nuspec: {artifact['name']}@{artifact['version']}"
            )
        nuspec_path = _package_regular_file(
            package_dir,
            nuspecs[0],
            label="nuspec",
        )
        if nuspec_path.read_bytes() != locked_nuspec_bytes:
            raise ValueError(
                f"restored NuGet package nuspec differs from locked nupkg payload: "
                f"{artifact['name']}@{artifact['version']}"
            )
        try:
            nuspec_root = ET.fromstring(locked_nuspec_bytes)
        except ET.ParseError as error:
            raise ValueError("locked NuGet nuspec is invalid") from error
        metadata_nodes = [
            node
            for node in nuspec_root.iter()
            if isinstance(node.tag, str)
            and node.tag.rsplit("}", 1)[-1] == "metadata"
        ]
        if len(metadata_nodes) != 1:
            raise ValueError(
                f"restored NuGet package lacks one metadata declaration: "
                f"{package_name}@{package_version}"
            )
        metadata = metadata_nodes[0]
        ids = [
            node
            for node in metadata
            if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "id"
        ]
        versions = [
            node
            for node in metadata
            if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "version"
        ]
        if len(ids) != 1 or (ids[0].text or "").strip() != package_name:
            raise ValueError(
                f"restored NuGet package nuspec id mismatch: "
                f"{package_name}@{package_version}"
            )
        if len(versions) != 1 or (versions[0].text or "").strip() != package_version:
            raise ValueError(
                f"restored NuGet package nuspec version mismatch: "
                f"{package_name}@{package_version}"
            )
        licenses = [
            node
            for node in metadata
            if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "license"
        ]
        if len(licenses) != 1:
            raise ValueError(
                f"restored NuGet package lacks one license declaration: {package_name}@{package_version}"
            )
        license_node = licenses[0]
        if license_node.attrib.get("type") != "file" or (license_node.text or "").strip() != record["license_file"]:
            raise ValueError(
                f"restored NuGet package license declaration mismatch: {package_name}@{package_version}"
            )


def _project_paths(root: Path, values: list[str]) -> list[Path] | None:
    if not values:
        return None
    projects: list[Path] = []
    for value in values:
        candidate = (root / value).resolve(strict=True)
        if not candidate.is_file() or not candidate.is_relative_to(root.resolve()):
            raise ValueError(f"project is outside repository: {value}")
        projects.append(candidate)
    return projects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-policy", action="store_true")
    parser.add_argument("--verify-restored", action="store_true")
    parser.add_argument("--packages-root")
    parser.add_argument("--project", action="append", default=[])
    args = parser.parse_args(argv)
    if args.check_policy == args.verify_restored:
        parser.error("choose exactly one verification mode")
    if args.check_policy:
        blockers = package_rights_blockers(ROOT)
        if blockers:
            for blocker in blockers:
                print(blocker)
            return 1
        return 0
    if not args.packages_root:
        parser.error("--packages-root is required with --verify-restored")
    projects = _project_paths(ROOT, args.project)
    try:
        verify_restored_package_rights(
            Path(args.packages_root),
            root=ROOT,
            projects=projects,
        )
    except ValueError as error:
        print(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
