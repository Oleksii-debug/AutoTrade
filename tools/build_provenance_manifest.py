"""Build/check the deterministic AutoTrade release dependency/rights manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha1
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "provenance" / "release-dependency-manifest.json"
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^=\s]+)$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REPOSITORY_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
UTC_EVIDENCE_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)


def release_evidence_document(
    path: Path,
    *,
    label: str,
    expected_source_sha: str | None = None,
) -> tuple[bool, str | None]:
    """Require explicit evidence before a release gate can be treated as satisfied."""
    if not path.exists():
        return False, "missing"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "invalid_json"
    if not isinstance(value, dict):
        return False, "not_object"
    if value.get("qualified") is not True:
        return False, "not_qualified"
    if not isinstance(value.get("schema_version"), str) or not value["schema_version"].strip():
        return False, "missing_schema_version"
    source_sha = value.get("source_sha")
    if not isinstance(source_sha, str) or GIT_SHA.fullmatch(source_sha) is None:
        return False, "invalid_source_sha"
    if expected_source_sha is not None:
        if GIT_SHA.fullmatch(expected_source_sha) is None:
            raise ValueError("expected_source_sha must be a canonical 40-hex commit SHA")
        if source_sha != expected_source_sha:
            return False, "source_sha_mismatch"
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        return False, "missing_evidence_refs"

    seen_artifact_ids: set[str] = set()
    for item in refs:
        if not isinstance(item, dict):
            return False, "invalid_evidence_refs"
        artifact_id = item.get("artifact_id")
        digest = item.get("sha256")
        observed_at = item.get("observed_at")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or not isinstance(digest, str)
            or SHA256_ID.fullmatch(digest) is None
            or not isinstance(observed_at, str)
            or UTC_EVIDENCE_TIME.fullmatch(observed_at) is None
        ):
            return False, "invalid_evidence_refs"
        try:
            instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            return False, "invalid_evidence_refs"
        if instant.tzinfo is None or instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") != observed_at:
            return False, "invalid_evidence_refs"
        canonical_artifact_id = artifact_id.strip()
        if canonical_artifact_id in seen_artifact_ids:
            return False, "invalid_evidence_refs"
        seen_artifact_ids.add(canonical_artifact_id)
    return True, None


def dependency_advisory_evidence_document(
    path: Path,
    *,
    expected_dependency_graph: dict[str, object],
    expected_source_sha: str | None = None,
) -> tuple[bool, str | None]:
    """Require advisory evidence for the exact dependency graph under review."""
    qualified, reason = release_evidence_document(
        path,
        label="dependency advisory qualification",
        expected_source_sha=expected_source_sha,
    )
    if not qualified:
        return False, reason
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "invalid_json"
    if value.get("dependency_graph") != expected_dependency_graph:
        return False, "dependency_graph_mismatch"
    return True, None


def normalize_inspected_components(
    document: object,
) -> tuple[list[dict[str, str]], list[str]]:
    """Validate immutable third/first-party source identities before release use."""
    if not isinstance(document, dict):
        raise ValueError("components provenance document must be an object")
    raw_components = document.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ValueError("components provenance must contain a non-empty components list")

    required = (
        "name",
        "repository",
        "revision",
        "license",
        "adoption_state",
        "source_import_allowed",
    )
    components: list[dict[str, str]] = []
    unresolved_first_party: list[str] = []
    seen_names: set[str] = set()
    seen_source_identities: set[tuple[str, str]] = set()

    for raw in raw_components:
        if not isinstance(raw, dict):
            raise ValueError("component provenance entry must be an object")
        record: dict[str, str] = {}
        for key in required:
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"component {key} must be canonical non-empty text")
            record[key] = value

        if REPOSITORY_SLUG.fullmatch(record["repository"]) is None:
            raise ValueError(
                f"component repository is not owner/name: {record['repository']}"
            )
        if GIT_OBJECT_ID.fullmatch(record["revision"]) is None:
            raise ValueError(
                f"component revision is not a canonical Git object id: {record['name']}"
            )

        name_key = record["name"].casefold()
        if name_key in seen_names:
            raise ValueError(f"duplicate component name: {record['name']}")
        seen_names.add(name_key)

        source_identity = (record["repository"].casefold(), record["revision"])
        if source_identity in seen_source_identities:
            raise ValueError(
                "duplicate component source identity: "
                f"{record['repository']}@{record['revision']}"
            )
        seen_source_identities.add(source_identity)

        components.append(record)
        if record["license"].startswith("UNRESOLVED_"):
            unresolved_first_party.append(record["name"])

    components.sort(key=lambda value: value["name"].casefold())
    return components, sorted(unresolved_first_party)


def git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def python_dev_dependencies() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for raw in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"requirements-dev entry is not exactly pinned: {line}")
        result.append({"name": match.group(1), "version": match.group(2)})
    return sorted(result, key=lambda item: item["name"].lower())


def dotnet_package_dependencies() -> list[dict[str, str]]:
    packages: set[tuple[str, str]] = set()
    for project in sorted((ROOT / "src").rglob("*.csproj")):
        tree = ET.parse(project)
        for node in tree.findall(".//PackageReference"):
            name = node.attrib.get("Include") or node.attrib.get("Update")
            version = node.attrib.get("Version")
            if version is None:
                child = node.find("Version")
                version = child.text.strip() if child is not None and child.text else None
            if not name or not version:
                raise ValueError(
                    f"PackageReference must have exact Include/Version in {project.relative_to(ROOT)}"
                )
            if any(token in version for token in ("*", "[", "]", "(", ")")):
                raise ValueError(
                    f"PackageReference is not an exact version in {project.relative_to(ROOT)}: {name} {version}"
                )
            packages.add((name, version))
    return [
        {"name": name, "version": version}
        for name, version in sorted(packages, key=lambda item: item[0].lower())
    ]


def build_manifest() -> dict[str, object]:
    components_path = ROOT / "provenance" / "components.json"
    requirements_path = ROOT / "requirements-dev.txt"
    global_path = ROOT / "global.json"
    components_doc = json.loads(components_path.read_text(encoding="utf-8"))
    global_doc = json.loads(global_path.read_text(encoding="utf-8"))

    components, unresolved_first_party = normalize_inspected_components(
        components_doc
    )

    blockers: list[dict[str, object]] = []
    composition = ROOT / "provenance" / "release-composition.json"
    composition_ok, composition_reason = release_evidence_document(
        composition,
        label="release composition",
    )
    release_source_sha: str | None = None
    if composition_ok:
        release_source_sha = json.loads(
            composition.read_text(encoding="utf-8")
        )["source_sha"]
    if not composition_ok:
        blockers.append(
            {
                "code": (
                    "RELEASE_COMPOSITION_MISSING"
                    if composition_reason == "missing"
                    else "RELEASE_COMPOSITION_UNQUALIFIED"
                ),
                "detail": (
                    "Exact shipped source/package composition has not been locked."
                    if composition_reason == "missing"
                    else f"Release composition evidence is not qualified: {composition_reason}."
                ),
            }
        )
    rights = ROOT / "provenance" / "model-data-rights.json"
    rights_ok, rights_reason = release_evidence_document(
        rights,
        label="model/data rights",
        expected_source_sha=release_source_sha,
    )
    if not rights_ok:
        blockers.append(
            {
                "code": (
                    "MODEL_DATA_RIGHTS_MISSING"
                    if rights_reason == "missing"
                    else "MODEL_DATA_RIGHTS_UNQUALIFIED"
                ),
                "detail": (
                    "Exact model/data/news redistribution and use rights have not been locked."
                    if rights_reason == "missing"
                    else f"Model/data rights evidence is not qualified: {rights_reason}."
                ),
            }
        )
    if unresolved_first_party:
        blockers.append(
            {
                "code": "FIRST_PARTY_RIGHTS_UNRESOLVED",
                "components": sorted(unresolved_first_party),
                "detail": "Development authorization is recorded, but release distribution rights chain is unresolved.",
            }
        )

    python_dependencies = python_dev_dependencies()
    dotnet_packages = dotnet_package_dependencies()
    dependency_graph = {
        "python_development_dependencies": python_dependencies,
        "dotnet_package_dependencies": dotnet_packages,
        "inspected_components": components,
    }

    advisories = ROOT / "provenance" / "dependency-advisory-qualification.json"
    advisories_ok, advisories_reason = dependency_advisory_evidence_document(
        advisories,
        expected_dependency_graph=dependency_graph,
        expected_source_sha=release_source_sha,
    )
    if not advisories_ok:
        blockers.append(
            {
                "code": (
                    "DEPENDENCY_ADVISORY_EVIDENCE_MISSING"
                    if advisories_reason == "missing"
                    else "DEPENDENCY_ADVISORY_EVIDENCE_UNQUALIFIED"
                ),
                "detail": (
                    "Exact release dependency graph has no qualified vulnerability/advisory review."
                    if advisories_reason == "missing"
                    else f"Dependency advisory evidence is not qualified: {advisories_reason}."
                ),
            }
        )

    if dotnet_packages and not list((ROOT / "src").rglob("packages.lock.json")):
        blockers.append(
            {
                "code": "DOTNET_TRANSITIVE_LOCK_MISSING",
                "detail": "Release PackageReference dependencies exist without committed packages.lock.json files.",
            }
        )

    return {
        "schema_version": "1.0.0",
        "source_inventory": {
            "components_blob_sha": git_blob_sha(components_path),
            "requirements_dev_blob_sha": git_blob_sha(requirements_path),
            "global_json_blob_sha": git_blob_sha(global_path),
        },
        "dotnet_sdk": str(global_doc["sdk"]["version"]),
        "python_development_dependencies": python_dependencies,
        "dotnet_package_dependencies": dotnet_packages,
        "inspected_components": components,
        "blocking_issues": blockers,
        "release_eligible": not blockers,
    }


def rendered_manifest() -> str:
    return json.dumps(
        build_manifest(),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write == args.check:
        parser.error("choose exactly one of --write or --check")
    rendered = rendered_manifest()
    if args.write:
        OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
        return 0
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
        print(
            "release-dependency-manifest.json is stale; run "
            "python tools/build_provenance_manifest.py --write",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
