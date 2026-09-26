"""Fail-closed dependency and rights composition audit for WP-03.

This module does not approve dependencies. It produces a deterministic report
showing whether the checked source tree is release-composition qualified.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
import json
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
EXACT_PYTHON = re.compile(r"^[A-Za-z0-9_.-]+==[^=<>!~\s]+$")
EXACT_DOTNET_SDK = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
EXACT_NUGET = re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class CompositionReport:
    qualified: bool
    blockers: tuple[str, ...]
    exact_python_requirements: tuple[str, ...]
    exact_nuget_references: tuple[str, ...]
    dotnet_sdk: str

    def as_jsonable(self) -> dict:
        return asdict(self)


def is_exact_python_requirement(value: str) -> bool:
    if not isinstance(value, str) or EXACT_PYTHON.fullmatch(value) is None:
        return False
    _, version = value.split("==", 1)
    if any(token in version for token in ("*", ",", ";", "@", "/", "\\")):
        return False
    return re.fullmatch(
        r"[0-9]+(?:\.[0-9A-Za-z]+)+(?:[-+][0-9A-Za-z.-]+)?",
        version,
    ) is not None


def _meaningful_requirements(path: Path) -> list[str]:
    result: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        result.append(line)
    return result


def _python_blockers(root: Path) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    exact: list[str] = []
    for requirement in _meaningful_requirements(root / "requirements-dev.txt"):
        if is_exact_python_requirement(requirement):
            exact.append(requirement)
        else:
            blockers.append(f"NON_EXACT_PYTHON_REQUIREMENT:{requirement}")

    pyproject = root / "research" / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    in_build_requires = False
    build_requires_found = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("["):
            in_build_requires = stripped == "[build-system]"
            continue
        if in_build_requires and stripped.startswith("requires"):
            build_requires_found = True
            value = stripped.split("=", 1)[1].strip()
            try:
                items = json.loads(value)
            except json.JSONDecodeError:
                blockers.append("UNREADABLE_RESEARCH_BUILD_REQUIREMENTS")
                break
            for requirement in items:
                if not is_exact_python_requirement(requirement):
                    blockers.append(f"NON_EXACT_RESEARCH_BUILD_REQUIREMENT:{requirement}")
            break
    if not build_requires_found:
        blockers.append("MISSING_RESEARCH_BUILD_REQUIREMENTS")
    return blockers, exact


def _dotnet_dependency_lock_blockers(
    root: Path,
    package_projects: list[Path],
) -> list[str]:
    projects = sorted(set(package_projects))
    if not projects:
        return []

    blockers: list[str] = []
    for project in projects:
        if not (project.parent / "packages.lock.json").is_file():
            blockers.append(
                "DOTNET_PROJECT_LOCK_MISSING:"
                f"{project.relative_to(root).as_posix()}"
            )

    workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
    if not workflow.is_file():
        blockers.append("DOTNET_LOCKED_RESTORE_WORKFLOW_MISSING")
        return blockers

    workflow_text = workflow.read_text(encoding="utf-8")
    if '"src/**/packages.lock.json"' not in workflow_text:
        blockers.append("DOTNET_LOCK_WORKFLOW_PATH_MISSING")

    restore_commands: list[str] = []
    for raw in workflow_text.splitlines():
        command = raw.strip()
        if command.startswith("- "):
            command = command[2:].strip()
        if command.startswith("run: dotnet restore "):
            restore_commands.append(command)

    if not restore_commands:
        blockers.append("DOTNET_LOCKED_RESTORE_COMMAND_MISSING")
        return blockers

    for index, command in enumerate(restore_commands, start=1):
        if (
            "--locked-mode" not in command
            and "RestoreLockedMode=true" not in command
        ):
            blockers.append(
                "DOTNET_RESTORE_NOT_LOCKED:"
                f".github/workflows/dotnet-foundation.yml:{index}"
            )
    return blockers


def _dotnet_blockers(root: Path) -> tuple[list[str], list[str], str]:
    blockers: list[str] = []
    references: list[str] = []
    global_json = json.loads((root / "global.json").read_text(encoding="utf-8"))
    sdk = global_json.get("sdk", {})
    version = sdk.get("version")
    if not isinstance(version, str) or not EXACT_DOTNET_SDK.fullmatch(version):
        blockers.append("NON_EXACT_DOTNET_SDK_VERSION")
        version = "" if version is None else str(version)

    if sdk.get("rollForward") not in (None, "disable"):
        blockers.append(f"DOTNET_ROLL_FORWARD_NOT_DISABLED:{sdk.get('rollForward')}")

    package_projects: list[Path] = []
    for project in sorted((root / "src").rglob("*.csproj")):
        tree = ET.parse(project)
        package_nodes = tree.findall(".//PackageReference")
        if package_nodes:
            package_projects.append(project)
        for node in package_nodes:
            name = node.attrib.get("Include") or node.attrib.get("Update") or ""
            value = node.attrib.get("Version")
            if value is None:
                version_node = node.find("Version")
                value = version_node.text.strip() if version_node is not None and version_node.text else None
            identity = f"{name}@{value}"
            if not name or not value or not EXACT_NUGET.fullmatch(value):
                blockers.append(f"NON_EXACT_NUGET_REFERENCE:{project.relative_to(root)}:{identity}")
            else:
                references.append(identity)
    blockers.extend(_dotnet_dependency_lock_blockers(root, package_projects))
    return blockers, references, version


def _ci_runtime_blockers(root: Path) -> list[str]:
    blockers: list[str] = []
    exact_python = re.compile(r"^\d+\.\d+\.\d+$")
    workflows = root / ".github" / "workflows"
    for path in sorted(workflows.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        if "actions/setup-python@" not in text:
            continue
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped.startswith("python-version:"):
                continue
            value = stripped.split(":", 1)[1].strip()
            if value.startswith("${{"):
                continue
            literals: list[str]
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value.replace("'", '"'))
                except json.JSONDecodeError:
                    blockers.append(
                        f"UNREADABLE_CI_PYTHON_VERSION:{path.relative_to(root)}:{value}"
                    )
                    continue
                literals = [str(item) for item in parsed]
            else:
                literals = [value.strip("'\"")]
            for literal in literals:
                if not exact_python.fullmatch(literal):
                    blockers.append(
                        f"NON_EXACT_CI_PYTHON_VERSION:{path.relative_to(root)}:{literal}"
                    )
    return blockers


def _strict_json_document(text: str):
    """Parse JSON while rejecting duplicate object keys at every nesting level."""

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicate_keys)


def _repository_evidence_digest(
    root: Path,
    *,
    component_name: str,
    component: dict,
    digest_field: str,
    path_field: str,
) -> tuple[str | None, list[str]]:
    """Resolve one APPROVED evidence digest to exact in-repository bytes."""

    blockers: list[str] = []
    raw_path = component.get(path_field)
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or raw_path != raw_path.strip()
        or "\\" in raw_path
    ):
        return None, [
            f"APPROVED_COMPONENT_EVIDENCE_PATH_INVALID:{component_name}:{path_field}"
        ]

    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "provenance"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None, [
            f"APPROVED_COMPONENT_EVIDENCE_PATH_INVALID:{component_name}:{path_field}"
        ]

    candidate = root.joinpath(*relative.parts)
    # Release evidence must be repository bytes, not a symlink escape whose target
    # can change independently of the exact source revision.
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None, [
                f"APPROVED_COMPONENT_EVIDENCE_PATH_INVALID:{component_name}:{path_field}"
            ]
    try:
        provenance_root = (root / "provenance").resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None, [
            f"APPROVED_COMPONENT_EVIDENCE_MISSING:{component_name}:{path_field}"
        ]
    if not resolved.is_file() or not resolved.is_relative_to(provenance_root):
        return None, [
            f"APPROVED_COMPONENT_EVIDENCE_PATH_INVALID:{component_name}:{path_field}"
        ]

    try:
        payload = resolved.read_bytes()
    except OSError:
        return None, [
            f"APPROVED_COMPONENT_EVIDENCE_MISSING:{component_name}:{path_field}"
        ]
    if not payload:
        blockers.append(
            f"APPROVED_COMPONENT_EVIDENCE_EMPTY:{component_name}:{path_field}"
        )
        return None, blockers

    actual = "sha256:" + sha256(payload).hexdigest()
    expected = component.get(digest_field)
    if expected != actual:
        blockers.append(
            f"APPROVED_COMPONENT_EVIDENCE_DIGEST_MISMATCH:{component_name}:{digest_field}"
        )
    return actual, blockers


def _rights_blockers(root: Path) -> list[str]:
    """Fail closed on machine release state, not only descriptive free text.

    Human-readable source_import_allowed values remain useful diagnostics, but
    release qualification must be impossible to obtain by changing those words
    while the canonical machine state is still BLOCKED or malformed.
    """

    blockers: list[str] = []
    try:
        document = _strict_json_document(
            (root / "provenance" / "components.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return ["COMPONENT_INVENTORY_INVALID_JSON"]
    if not isinstance(document, dict):
        return ["COMPONENT_INVENTORY_INVALID_ROOT"]
    components = document.get("components")
    if not isinstance(components, list) or not components:
        return ["COMPONENT_INVENTORY_MISSING_OR_EMPTY"]

    seen_names: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    revision_pattern = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    digest_pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    repository_pattern = re.compile(
        r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )

    for index, component in enumerate(components):
        if not isinstance(component, dict):
            blockers.append(f"INVALID_COMPONENT_RECORD:{index}")
            continue

        raw_name = component.get("name")
        name = raw_name if isinstance(raw_name, str) else f"<unnamed:{index}>"
        if (
            not isinstance(raw_name, str)
            or not raw_name
            or raw_name != raw_name.strip()
        ):
            blockers.append(f"INVALID_COMPONENT_NAME:{index}")
        elif raw_name in seen_names:
            blockers.append(f"DUPLICATE_COMPONENT_NAME:{raw_name}")
        else:
            seen_names.add(raw_name)

        repository = component.get("repository")
        revision = component.get("revision")
        if (
            not isinstance(repository, str)
            or repository != repository.strip()
            or repository_pattern.fullmatch(repository) is None
            or not isinstance(revision, str)
            or revision_pattern.fullmatch(revision) is None
        ):
            blockers.append(f"INVALID_COMPONENT_SOURCE_IDENTITY:{name}")
        else:
            source_identity = (repository, revision)
            if source_identity in seen_sources:
                blockers.append(f"DUPLICATE_COMPONENT_SOURCE:{name}")
            else:
                seen_sources.add(source_identity)

        license_value = component.get("license")
        if not isinstance(license_value, str) or not license_value.strip():
            blockers.append(f"MISSING_COMPONENT_RIGHTS:{name}")
            license_text = ""
        else:
            license_text = license_value
            if "UNRESOLVED" in license_text:
                blockers.append(f"UNRESOLVED_COMPONENT_RIGHTS:{name}")

        import_disposition = component.get("source_import_allowed")
        if not isinstance(import_disposition, str) or not import_disposition.strip():
            blockers.append(f"MISSING_SOURCE_IMPORT_DISPOSITION:{name}")
            import_text = ""
        else:
            import_text = import_disposition
            if (
                "PENDING_" in import_text
                or "STILL_REQUIRES_WP03_RECORD" in import_text
                or "AFTER_EXACT_" in import_text
                or "AFTER_STABLE_" in import_text
            ):
                blockers.append(f"UNQUALIFIED_SOURCE_COMPOSITION:{name}")

        release_state = component.get("release_distribution_state")
        if release_state not in {"BLOCKED", "APPROVED"}:
            blockers.append(f"INVALID_RELEASE_DISTRIBUTION_STATE:{name}")
            continue
        if release_state != "APPROVED":
            blockers.append(
                f"COMPONENT_RELEASE_DISTRIBUTION_NOT_APPROVED:{name}"
            )
            continue

        # APPROVED is a privileged machine state. A digest-shaped string is
        # not evidence. Each digest must resolve to exact, non-symlink repository
        # bytes under provenance/ so the exact source revision authenticates the
        # evidence location and this gate recomputes content identity itself.
        if "UNRESOLVED" in license_text:
            blockers.append(f"APPROVED_COMPONENT_RIGHTS_UNRESOLVED:{name}")
        evidence_fields = (
            ("dependency_graph_sha256", "dependency_graph_evidence_path"),
            ("notice_sha256", "notice_evidence_path"),
            ("advisory_review_sha256", "advisory_review_evidence_path"),
        )
        for digest_field, path_field in evidence_fields:
            value = component.get(digest_field)
            if not isinstance(value, str) or digest_pattern.fullmatch(value) is None:
                blockers.append(
                    f"APPROVED_COMPONENT_EVIDENCE_INVALID:{name}:{digest_field}"
                )
                continue
            _, evidence_blockers = _repository_evidence_digest(
                root,
                component_name=name,
                component=component,
                digest_field=digest_field,
                path_field=path_field,
            )
            blockers.extend(evidence_blockers)
    return blockers


def audit_composition(root: Path = ROOT) -> CompositionReport:
    python_blockers, python_exact = _python_blockers(root)
    dotnet_blockers, nuget_exact, sdk_version = _dotnet_blockers(root)
    blockers = sorted(
        set(
            python_blockers
            + dotnet_blockers
            + _ci_runtime_blockers(root)
            + _rights_blockers(root)
        )
    )
    return CompositionReport(
        qualified=not blockers,
        blockers=tuple(blockers),
        exact_python_requirements=tuple(sorted(python_exact)),
        exact_nuget_references=tuple(sorted(nuget_exact)),
        dotnet_sdk=sdk_version,
    )


def qualification_exit_code(
    report: CompositionReport, *, require_qualified: bool
) -> int:
    """Return a CI-safe exit code without changing report generation semantics."""

    if not isinstance(report, CompositionReport):
        raise TypeError("report must be CompositionReport")
    if type(require_qualified) is not bool:
        raise TypeError("require_qualified must be boolean")
    return 1 if require_qualified and not report.qualified else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit exact dependency and rights composition for WP-03."
    )
    parser.add_argument(
        "--require-qualified",
        action="store_true",
        help=(
            "fail with a non-zero exit code unless the complete composition "
            "is qualified; omit for deterministic blocker-report mode"
        ),
    )
    args = parser.parse_args(argv)
    report = audit_composition()
    print(json.dumps(report.as_jsonable(), ensure_ascii=False, indent=2, sort_keys=True))
    return qualification_exit_code(
        report,
        require_qualified=args.require_qualified,
    )


if __name__ == "__main__":
    raise SystemExit(main())
