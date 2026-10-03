"""Build/check the deterministic AutoTrade release dependency/rights manifest."""

from __future__ import annotations

import argparse
import ast
import dis
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
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

QUALIFICATION_TRUST_POLICY_COMPONENT_ID = "autotrade-qualification-trust-policy"
QUALIFICATION_TRUST_POLICY_COMPONENT_KIND = "qualification-trust-policy"
QUALIFICATION_TRUST_POLICY_COMPONENT_PATH = (
    "mvp/autotrade_mvp/qualification_trust_policy.json"
)
QUALIFICATION_TRUST_POLICY_COMPONENT_VERSION = "source-controlled"
QUALIFICATION_TRUST_POLICY_PIN_SOURCE_PATH = (
    "mvp/autotrade_mvp/qualification_attestation.py"
)
_QUALIFICATION_TRUST_POLICY_PIN_SOURCE_MAX_BYTES = 1024 * 1024


def _trusted_git_candidate_paths() -> tuple[Path, ...]:
    """Return fail-closed OS-managed Git locations without consulting PATH."""

    if os.name == "nt":
        return (
            Path(r"C:\\Program Files\\Git\\cmd\\git.exe"),
            Path(r"C:\\Program Files\\Git\\bin\\git.exe"),
        )
    return (Path("/usr/bin/git"), Path("/bin/git"))


def _trusted_git_executable(*, source_root: Path) -> str:
    """Resolve Git independently of caller PATH and source-checkout content."""

    resolved_source_root = source_root.resolve(strict=True)
    for candidate in _trusted_git_candidate_paths():
        try:
            executable = candidate.resolve(strict=True)
        except OSError:
            continue
        if not executable.is_file():
            continue
        try:
            executable.relative_to(resolved_source_root)
        except ValueError:
            return os.fspath(executable)
        raise ValueError("trusted Git executable must not originate from source_root")
    raise ValueError("trusted Git executable is unavailable at an OS-managed location")


def _trusted_git_environment() -> dict[str, str]:
    """Run exact-object reads without caller-selected Git/process authority."""

    environment = {
        key: value
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC")
        if (value := os.environ.get(key))
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    return environment


def _trusted_git(
    *args: str,
    source_root: Path,
    text: bool = False,
) -> bytes | str:
    """Execute one bounded Git object query under the canonical clean process cut."""

    source_root = source_root.resolve(strict=True)
    executable = _trusted_git_executable(source_root=source_root)
    try:
        completed = subprocess.run(
            [executable, *args],
            cwd=source_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
            encoding="utf-8" if text else None,
            timeout=10,
            env=_trusted_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("exact Git source query is unavailable") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() if text else completed.stderr.decode(
            "utf-8", errors="replace"
        ).strip()
        raise ValueError(
            f"exact Git source query failed: {detail or args[0]}"
        )
    return completed.stdout


def _exact_git_blob(
    *,
    source_root: Path,
    source_sha: str,
    relative_path: str,
) -> tuple[bytes, str]:
    """Read one exact blob and its object id from the selected source commit."""

    if GIT_SHA.fullmatch(source_sha) is None:
        raise ValueError("source_sha must be a canonical 40-hex commit SHA")
    pure = PurePosixPath(relative_path)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != relative_path
    ):
        raise ValueError("Git source path must be canonical and repository-relative")

    top = _trusted_git("rev-parse", "--show-toplevel", source_root=source_root, text=True)
    assert isinstance(top, str)
    try:
        top_path = Path(top.strip()).resolve(strict=True)
    except OSError as error:
        raise ValueError("Git source root cannot be verified") from error
    if top_path != source_root.resolve(strict=True):
        raise ValueError("source_root must be the exact Git top-level")

    _trusted_git("cat-file", "-e", f"{source_sha}^{{commit}}", source_root=source_root)
    object_id_raw = _trusted_git(
        "rev-parse",
        f"{source_sha}:{relative_path}",
        source_root=source_root,
        text=True,
    )
    assert isinstance(object_id_raw, str)
    object_id = object_id_raw.strip()
    if GIT_OBJECT_ID.fullmatch(object_id) is None:
        raise ValueError("exact Git source lookup returned noncanonical object id")
    object_type = _trusted_git("cat-file", "-t", object_id, source_root=source_root, text=True)
    assert isinstance(object_type, str)
    if object_type.strip() != "blob":
        raise ValueError("exact Git source object is not a blob")

    tree_entry = _trusted_git(
        "ls-tree",
        "-z",
        source_sha,
        "--",
        relative_path,
        source_root=source_root,
    )
    assert isinstance(tree_entry, bytes)
    expected_path = relative_path.encode("utf-8") + b"\x00"
    metadata, separator, tree_path = tree_entry.partition(b"\t")
    metadata_parts = metadata.split()
    if (
        separator != b"\t"
        or tree_path != expected_path
        or len(metadata_parts) != 3
        or metadata_parts[0] not in {b"100644", b"100755"}
        or metadata_parts[1] != b"blob"
        or metadata_parts[2].decode("ascii", errors="ignore") != object_id
    ):
        raise ValueError("exact Git source path is not a regular blob")

    object_size_raw = _trusted_git(
        "cat-file",
        "-s",
        object_id,
        source_root=source_root,
        text=True,
    )
    assert isinstance(object_size_raw, str)
    object_size_text = object_size_raw.strip()
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", object_size_text) is None:
        raise ValueError("exact Git source object size is invalid")
    if int(object_size_text) > _QUALIFICATION_TRUST_POLICY_PIN_SOURCE_MAX_BYTES:
        raise ValueError("exact Git source object exceeds bounded size")

    raw = _trusted_git("cat-file", "blob", object_id, source_root=source_root)
    assert isinstance(raw, bytes)
    if len(raw) != int(object_size_text):
        raise ValueError("exact Git source object size changed during read")
    return raw, object_id


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


def qualification_trust_policy_digest_from_bytes(
    raw: bytes,
    *,
    source_name: str = "qualification_attestation.py",
) -> str | None:
    """Parse the one literal packaged-policy pin without executing product code."""

    if type(raw) is not bytes:
        raise TypeError("qualification trust policy pin source must be exact bytes")
    try:
        source = raw.decode("utf-8", errors="strict")
        module = ast.parse(source, filename=source_name)
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ValueError(
            "qualification trust policy pin source is unavailable or invalid"
        ) from error

    values: list[object] = []
    invalid_binding = object()
    target_name = "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256"

    try:
        module_code = compile(source, source_name, "exec")
    except (SyntaxError, ValueError, TypeError) as error:
        raise ValueError(
            "qualification trust policy pin source is unavailable or invalid"
        ) from error
    module_bindings = [
        instruction
        for instruction in dis.get_instructions(module_code)
        if instruction.argval == target_name
        and instruction.opname
        in {"STORE_NAME", "STORE_GLOBAL", "DELETE_NAME", "DELETE_GLOBAL"}
    ]
    if len(module_bindings) != 1:
        raise ValueError(
            "qualification trust policy pin must have one literal source definition"
        )

    for statement in module.body:
        stores = [
            node
            for node in ast.walk(statement)
            if isinstance(node, ast.Name)
            and node.id == target_name
            and isinstance(node.ctx, ast.Store)
        ]
        if not stores:
            continue
        if (
            len(stores) == 1
            and isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == target_name
        ):
            value = statement.value
            values.append(
                value.value if isinstance(value, ast.Constant) else invalid_binding
            )
            continue
        if (
            len(stores) == 1
            and isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == target_name
        ):
            value = statement.value
            values.append(
                value.value if isinstance(value, ast.Constant) else invalid_binding
            )
            continue
        values.append(invalid_binding)

    if len(values) != 1 or values[0] is invalid_binding:
        raise ValueError(
            "qualification trust policy pin must have one literal source definition"
        )
    value = values[0]
    if value is None:
        return None
    if not isinstance(value, str) or SHA256_ID.fullmatch(value) is None:
        raise ValueError(
            "qualification trust policy pin must be None or canonical SHA-256"
        )
    return value


def qualification_trust_policy_digest_from_source(path: Path) -> str | None:
    """Parse a local source file for unit/source-checkout diagnostics only."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(
            "qualification trust policy pin source is unavailable or invalid"
        ) from error
    return qualification_trust_policy_digest_from_bytes(
        raw,
        source_name=str(path),
    )


def qualification_trust_policy_digest_from_git_source(
    *,
    source_root: Path,
    source_sha: str,
) -> tuple[str | None, str]:
    """Load the pin from the exact release source object, never the working tree."""

    raw, object_id = _exact_git_blob(
        source_root=source_root,
        source_sha=source_sha,
        relative_path=QUALIFICATION_TRUST_POLICY_PIN_SOURCE_PATH,
    )
    digest = qualification_trust_policy_digest_from_bytes(
        raw,
        source_name=f"{source_sha}:{QUALIFICATION_TRUST_POLICY_PIN_SOURCE_PATH}",
    )
    return digest, object_id


def qualification_trust_policy_composition(
    document: object,
    *,
    expected_digest: str | None,
) -> tuple[bool, str | None, str | None]:
    """Validate the canonical qualification-policy component in release composition.

    The composition may contain unrelated product components, but the
    qualification policy has one canonical identity. Its digest comes from the
    source-controlled product pin; release metadata cannot select or override it.
    """

    if expected_digest is None:
        return False, "policy_pin_missing", None
    if not isinstance(expected_digest, str) or SHA256_ID.fullmatch(expected_digest) is None:
        raise ValueError(
            "expected qualification trust policy digest must be canonical SHA-256"
        )
    if not isinstance(document, dict):
        return False, "composition_not_object", None
    components = document.get("components")
    if not isinstance(components, list):
        return False, "components_missing", None

    matches: list[dict[str, object]] = []
    for item in components:
        if not isinstance(item, dict):
            return False, "component_not_object", None
        if (
            item.get("component_id") == QUALIFICATION_TRUST_POLICY_COMPONENT_ID
            or item.get("path") == QUALIFICATION_TRUST_POLICY_COMPONENT_PATH
            or item.get("kind") == QUALIFICATION_TRUST_POLICY_COMPONENT_KIND
        ):
            matches.append(item)

    if not matches:
        return False, "component_missing", None
    if len(matches) != 1:
        return False, "component_ambiguous", None

    expected = {
        "component_id": QUALIFICATION_TRUST_POLICY_COMPONENT_ID,
        "kind": QUALIFICATION_TRUST_POLICY_COMPONENT_KIND,
        "path": QUALIFICATION_TRUST_POLICY_COMPONENT_PATH,
        "version": QUALIFICATION_TRUST_POLICY_COMPONENT_VERSION,
        "sha256": expected_digest,
    }
    candidate = matches[0]
    if frozenset(candidate) != frozenset(expected):
        return False, "component_fields_mismatch", None
    for key, value in expected.items():
        if candidate.get(key) != value:
            return False, f"{key}_mismatch", None
    return True, None, expected_digest


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
        "release_distribution_state",
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

        release_state = record["release_distribution_state"]
        if release_state not in {"BLOCKED", "APPROVED"}:
            raise ValueError(
                "component release_distribution_state must be BLOCKED or APPROVED: "
                f"{record['name']}"
            )
        if release_state == "APPROVED":
            if record["license"].startswith("UNRESOLVED_"):
                raise ValueError(
                    f"release-approved component has unresolved license: {record['name']}"
                )
            for evidence_field in (
                "dependency_graph_sha256",
                "notice_sha256",
                "advisory_review_sha256",
            ):
                evidence_value = raw.get(evidence_field)
                if (
                    not isinstance(evidence_value, str)
                    or SHA256_ID.fullmatch(evidence_value) is None
                ):
                    raise ValueError(
                        f"release-approved component lacks canonical {evidence_field}: "
                        f"{record['name']}"
                    )
                record[evidence_field] = evidence_value

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
    """Return Git's canonical object identity for a repository file.

    Release provenance must identify the bytes Git would store after applying
    the repository's clean-filter and EOL attributes, not platform-specific
    working-tree bytes. The path is deliberately constrained to this
    repository so callers cannot mint provenance identities for unrelated
    filesystem content.
    """
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"provenance path is unavailable: {path}") from exc
    if not resolved.is_file():
        raise ValueError(f"provenance path is not a file: {path}")

    repository_root = ROOT.resolve()
    try:
        relative = resolved.relative_to(repository_root)
    except ValueError as exc:
        raise ValueError(
            f"provenance path must be inside repository: {path}"
        ) from exc

    try:
        completed = subprocess.run(
            [
                "git",
                "hash-object",
                f"--path={relative.as_posix()}",
                str(resolved),
            ],
            cwd=repository_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("git hash-object is unavailable") from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(
            f"git hash-object failed for {relative.as_posix()}: {detail}"
        )
    object_id = completed.stdout.strip()
    if GIT_OBJECT_ID.fullmatch(object_id) is None:
        raise RuntimeError(
            f"git hash-object returned a noncanonical object id for {relative.as_posix()}"
        )
    return object_id


def python_dev_dependencies() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    logical = ""
    for raw in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        logical = f"{logical} {fragment}".strip()
        if continued:
            continue

        tokens = logical.split()
        match = PIN.fullmatch(tokens[0])
        if match is None:
            raise ValueError(
                f"requirements-dev entry is not exactly pinned: {tokens[0]}"
            )
        hashes: list[str] = []
        for token in tokens[1:]:
            if not token.startswith("--hash="):
                raise ValueError(f"requirements-dev option is unsupported: {token}")
            digest = token.removeprefix("--hash=")
            if SHA256_ID.fullmatch(digest) is None:
                raise ValueError(
                    f"requirements-dev hash is not canonical SHA-256: {token}"
                )
            hashes.append(digest)
        if not hashes:
            raise ValueError(
                f"requirements-dev entry has no artifact hash: {tokens[0]}"
            )
        if len(hashes) != len(set(hashes)):
            raise ValueError(
                f"requirements-dev entry repeats an artifact hash: {tokens[0]}"
            )
        result.append(
            {
                "name": match.group(1),
                "version": match.group(2),
                "hashes": sorted(hashes),
            }
        )
        logical = ""
    if logical:
        raise ValueError("requirements-dev has an unterminated continuation")
    return sorted(result, key=lambda item: str(item["name"]).lower())


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


def dotnet_package_projects() -> list[Path]:
    projects: list[Path] = []
    for project in sorted((ROOT / "src").rglob("*.csproj")):
        tree = ET.parse(project)
        if tree.findall(".//PackageReference"):
            projects.append(project)
    return projects


def build_manifest() -> dict[str, object]:
    components_path = ROOT / "provenance" / "components.json"
    requirements_path = ROOT / "requirements-dev.txt"
    research_pyproject_path = ROOT / "research" / "pyproject.toml"
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
    release_policy_digest: str | None = None
    release_policy_pin_blob_sha: str | None = None
    if composition_ok:
        composition_document = json.loads(
            composition.read_text(encoding="utf-8")
        )
        release_source_sha = composition_document["source_sha"]

        try:
            (
                expected_policy_digest,
                release_policy_pin_blob_sha,
            ) = qualification_trust_policy_digest_from_git_source(
                source_root=ROOT,
                source_sha=release_source_sha,
            )
        except ValueError:
            policy_ok = False
            policy_reason = "policy_pin_source_invalid"
        else:
            policy_ok, policy_reason, release_policy_digest = (
                qualification_trust_policy_composition(
                    composition_document,
                    expected_digest=expected_policy_digest,
                )
            )
        if not policy_ok:
            blockers.append(
                {
                    "code": (
                        "QUALIFICATION_TRUST_POLICY_PIN_MISSING"
                        if policy_reason == "policy_pin_missing"
                        else "QUALIFICATION_TRUST_POLICY_COMPONENT_MISSING"
                        if policy_reason == "component_missing"
                        else "QUALIFICATION_TRUST_POLICY_COMPONENT_UNQUALIFIED"
                    ),
                    "detail": (
                        "Authenticated release composition does not bind the "
                        "canonical qualification trust policy: "
                        f"{policy_reason}."
                    ),
                }
            )
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

    blocked_release_components = sorted(
        component["name"]
        for component in components
        if component["release_distribution_state"] != "APPROVED"
    )
    if blocked_release_components:
        blockers.append(
            {
                "code": "COMPONENT_RELEASE_DISTRIBUTION_BLOCKED",
                "components": blocked_release_components,
                "detail": "One or more inspected components are not machine-approved for release distribution.",
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

    dotnet_projects = dotnet_package_projects()
    missing_dotnet_locks = [
        project.relative_to(ROOT).as_posix()
        for project in dotnet_projects
        if not (project.parent / "packages.lock.json").is_file()
    ]
    if missing_dotnet_locks:
        blockers.append(
            {
                "code": "DOTNET_TRANSITIVE_LOCK_MISSING",
                "projects": missing_dotnet_locks,
                "detail": (
                    "Each release project with PackageReference dependencies "
                    "requires its own committed sibling packages.lock.json."
                ),
            }
        )

    if dotnet_projects:
        foundation = ROOT / ".github" / "workflows" / "dotnet-foundation.yml"
        if not foundation.is_file():
            blockers.append(
                {
                    "code": "DOTNET_LOCKED_RESTORE_WORKFLOW_MISSING",
                    "detail": "NuGet dependencies require the canonical .NET restore workflow.",
                }
            )
        else:
            foundation_text = foundation.read_text(encoding="utf-8")
            if '"src/**/packages.lock.json"' not in foundation_text:
                blockers.append(
                    {
                        "code": "DOTNET_LOCK_WORKFLOW_PATH_MISSING",
                        "detail": (
                            "NuGet lock-file changes must trigger the canonical "
                            ".NET workflow."
                        ),
                    }
                )
            restore_commands: list[str] = []
            for raw in foundation_text.splitlines():
                command = raw.strip()
                if command.startswith("- "):
                    command = command[2:].strip()
                if command.startswith("run: dotnet restore "):
                    restore_commands.append(command)
            if not restore_commands:
                blockers.append(
                    {
                        "code": "DOTNET_LOCKED_RESTORE_COMMAND_MISSING",
                        "detail": (
                            "NuGet dependencies require an explicit canonical "
                            "dotnet restore command."
                        ),
                    }
                )
            elif any(
                "--locked-mode" not in command
                and "RestoreLockedMode=true" not in command
                for command in restore_commands
            ):
                blockers.append(
                    {
                        "code": "DOTNET_RESTORE_NOT_LOCKED",
                        "detail": (
                            "Every canonical dotnet restore must enforce the "
                            "committed NuGet dependency graph."
                        ),
                    }
                )

    source_inventory = {
        "components_blob_sha": git_blob_sha(components_path),
        "requirements_dev_blob_sha": git_blob_sha(requirements_path),
        "research_pyproject_blob_sha": git_blob_sha(research_pyproject_path),
        "global_json_blob_sha": git_blob_sha(global_path),
    }
    if release_policy_digest is not None:
        if release_policy_pin_blob_sha is None:
            raise RuntimeError(
                "qualified release policy lacks exact-source pin blob identity"
            )
        source_inventory["qualification_attestation_blob_sha"] = (
            release_policy_pin_blob_sha
        )
        source_inventory["qualification_trust_policy_sha256"] = (
            release_policy_digest
        )

    return {
        "schema_version": "1.0.0",
        "source_inventory": source_inventory,
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
