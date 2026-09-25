"""Fail-closed dependency and rights composition audit for WP-03.

This module does not approve dependencies. It produces a deterministic report
showing whether the checked source tree is release-composition qualified.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
import json
from pathlib import Path
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

    for project in sorted((root / "src").rglob("*.csproj")):
        tree = ET.parse(project)
        for node in tree.findall(".//PackageReference"):
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


def _rights_blockers(root: Path) -> list[str]:
    blockers: list[str] = []
    document = json.loads((root / "provenance" / "components.json").read_text(encoding="utf-8"))
    for component in document.get("components", []):
        name = component.get("name", "<unnamed>")
        license_value = str(component.get("license", ""))
        import_disposition = str(component.get("source_import_allowed", ""))
        if "UNRESOLVED" in license_value:
            blockers.append(f"UNRESOLVED_COMPONENT_RIGHTS:{name}")
        if (
            "PENDING_" in import_disposition
            or "STILL_REQUIRES_WP03_RECORD" in import_disposition
            or "AFTER_EXACT_" in import_disposition
            or "AFTER_STABLE_" in import_disposition
        ):
            blockers.append(f"UNQUALIFIED_SOURCE_COMPOSITION:{name}")
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
