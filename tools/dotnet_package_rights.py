from __future__ import annotations

import argparse
import base64
import binascii
import json
from pathlib import Path, PurePosixPath
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
    selected = _package_projects(root) if projects is None else sorted(set(projects))
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
            "expected_license_text_path": expected_path.relative_to(root).as_posix(),
            "notice_file": notice_file,
        }
        key = _artifact_key(record)
        if key in seen:
            raise ValueError(f"duplicate NuGet package-rights record: {name}@{version}")
        seen.add(key)
        records.append(record)
    return sorted(records, key=_artifact_key)


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
    return sorted(blockers)


def _normalized_license_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"license text is not UTF-8: {path}") from error
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip() + "\n"


def verify_restored_package_rights(
    packages_root: Path,
    *,
    root: Path = ROOT,
    projects: list[Path] | None = None,
) -> None:
    if not packages_root.is_dir():
        raise ValueError("NuGet global-packages root is unavailable")
    artifacts = locked_package_artifacts(root, projects=projects)
    records = {_artifact_key(item): item for item in package_rights_records(root)}
    for artifact in artifacts:
        key = _artifact_key(artifact)
        record = records.get(key)
        if record is None:
            raise ValueError(
                f"NuGet package rights are missing for {artifact['name']}@{artifact['version']}"
            )
        package_dir = packages_root / artifact["name"].casefold() / artifact["version"].casefold()
        if not package_dir.is_dir():
            raise ValueError(
                f"restored NuGet package is unavailable: {artifact['name']}@{artifact['version']}"
            )
        sha_files = tuple(package_dir.glob("*.nupkg.sha512"))
        if len(sha_files) != 1:
            raise ValueError(
                f"restored NuGet package lacks one SHA-512 authority: {artifact['name']}@{artifact['version']}"
            )
        restored_hash = sha_files[0].read_text(encoding="ascii").strip()
        if restored_hash != artifact["content_hash_sha512_base64"]:
            raise ValueError(
                f"restored NuGet package content hash mismatch: {artifact['name']}@{artifact['version']}"
            )
        license_path = package_dir / record["license_file"]
        notice_path = package_dir / record["notice_file"]
        if not license_path.is_file():
            raise ValueError(
                f"restored NuGet package license is missing: {artifact['name']}@{artifact['version']}"
            )
        if not notice_path.is_file() or not notice_path.read_bytes():
            raise ValueError(
                f"restored NuGet package notice is missing: {artifact['name']}@{artifact['version']}"
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
        try:
            tree = ET.parse(nuspecs[0])
        except (OSError, ET.ParseError) as error:
            raise ValueError("restored NuGet nuspec is invalid") from error
        licenses = [
            node
            for node in tree.iter()
            if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1] == "license"
        ]
        if len(licenses) != 1:
            raise ValueError(
                f"restored NuGet package lacks one license declaration: {artifact['name']}@{artifact['version']}"
            )
        license_node = licenses[0]
        if license_node.attrib.get("type") != "file" or (license_node.text or "").strip() != record["license_file"]:
            raise ValueError(
                f"restored NuGet package license declaration mismatch: {artifact['name']}@{artifact['version']}"
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
