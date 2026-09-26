"""Build a deterministic AutoTrade Windows bundle without granting release authority."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROVENANCE = ROOT / "provenance" / "release-dependency-manifest.json"
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
CANONICAL_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
FORBIDDEN_BASENAMES = {
    ".env",
    "credentials.json",
    "secrets.json",
    "secret.json",
    "api-keys.json",
    "tokens.json",
}
FORBIDDEN_SUFFIXES = {
    ".key",
    ".pfx",
    ".p12",
    ".pem",
    ".jks",
    ".keystore",
}
FORBIDDEN_PREFIXES = (
    ".env.",
)
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class BundleError(ValueError):
    pass


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BundleError(f"{name} is required")
    return value.strip()


def _safe_relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    posix = PurePosixPath(*relative.parts).as_posix()
    if posix.startswith("../") or posix == "..":
        raise BundleError("bundle input escaped staging root")
    return posix


def _windows_path_key(relative: str) -> str:
    normalized: list[str] = []
    for part in PurePosixPath(relative).parts:
        if part.endswith((" ", ".")):
            raise BundleError(
                f"bundle contains Windows-unsafe trailing space/dot segment: {relative}"
            )
        if ":" in part:
            raise BundleError(
                f"bundle contains Windows alternate-data-stream path: {relative}"
            )
        stem = part.split(".", 1)[0].casefold()
        if stem in WINDOWS_RESERVED_STEMS:
            raise BundleError(
                f"bundle contains Windows reserved device name: {relative}"
            )
        normalized.append(part.casefold())
    return "/".join(normalized)


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in FORBIDDEN_BASENAMES:
        return True
    if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
        return True
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    lowered_parts = {part.lower() for part in path.parts}
    return bool(lowered_parts & {"secrets", "credentials", "private-keys"})


def _looks_like_credential_vault(data: bytes) -> bool:
    """Detect the existing AutoTrade protected-vault shape by content.

    Filename/path gates are necessary but insufficient: a copied credential
    vault can be renamed before staging.  Detection intentionally requires the
    characteristic owner/ciphertext record shape to avoid treating arbitrary
    JSON documents as secrets.
    """

    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    records = value.get("records")
    if not isinstance(records, dict) or not records:
        return False
    for record in records.values():
        if not isinstance(record, dict):
            continue
        if {"handle", "owner_identity", "ciphertext", "active"} <= set(record):
            return True
    return False


def _reject_sensitive_content(relative: str, data: bytes) -> None:
    if any(marker in data for marker in PRIVATE_KEY_MARKERS):
        raise BundleError(
            f"private-key material is forbidden in bundles: {relative}"
        )
    if _looks_like_credential_vault(data):
        raise BundleError(
            f"credential-vault content is forbidden in bundles: {relative}"
        )


def _collect(staging: Path) -> list[tuple[str, Path, bytes]]:
    if not staging.is_dir():
        raise BundleError("staging must be an existing directory")
    collected: list[tuple[str, Path, bytes]] = []
    windows_names: dict[str, str] = {}
    for path in sorted(staging.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise BundleError(f"symlinks are forbidden in bundles: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleError(f"unsupported filesystem entry: {path}")
        relative = _safe_relative(path, staging)
        windows_key = _windows_path_key(relative)
        previous = windows_names.get(windows_key)
        if previous is not None and previous != relative:
            raise BundleError(
                "bundle contains Windows path collision: "
                f"{previous} conflicts with {relative}"
            )
        windows_names[windows_key] = relative
        if _is_sensitive(PurePosixPath(relative)):
            raise BundleError(f"sensitive path is forbidden in bundles: {relative}")
        data = path.read_bytes()
        _reject_sensitive_content(relative, data)
        collected.append((relative, path, data))
    if not collected:
        raise BundleError("staging directory contains no files")
    return collected


def _load_provenance(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError("release provenance manifest is missing or invalid") from error
    if not isinstance(value, dict):
        raise BundleError("release provenance manifest must be an object")
    if not isinstance(value.get("blocking_issues"), list):
        raise BundleError("release provenance manifest has no blocking_issues array")
    if not isinstance(value.get("release_eligible"), bool):
        raise BundleError("release provenance manifest has no release_eligible boolean")
    if value["release_eligible"] is True and value["blocking_issues"]:
        raise BundleError(
            "release provenance manifest is inconsistent: eligible state has blockers"
        )
    return value, "sha256:" + sha256(raw).hexdigest()


def _entry_metadata(relative: str, data: bytes) -> dict[str, object]:
    return {
        "path": relative,
        "sha256": "sha256:" + sha256(data).hexdigest(),
        "size": len(data),
    }


def _canonical_digest(value: object, *, name: str) -> str:
    digest = _required_text(value, name=name)
    if CANONICAL_SHA256.fullmatch(digest) is None:
        raise BundleError(f"{name} must be a canonical lowercase sha256: digest")
    return digest


def _composition_component(
    value: object,
    *,
    index: int,
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BundleError(f"composition components[{index}] must be an object")
    required = {"component_id", "kind", "path", "version", "sha256"}
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise BundleError(
            f"composition components[{index}] has invalid fields"
            + (": " + "; ".join(detail) if detail else "")
        )
    component_id = _required_text(value["component_id"], name="component_id")
    kind = _required_text(value["kind"], name="kind")
    version = _required_text(value["version"], name="component version")
    path = _required_text(value["path"], name="component path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != path:
        raise BundleError(f"composition component path is unsafe: {path}")
    _windows_path_key(path)
    return {
        "component_id": component_id,
        "kind": kind,
        "path": path,
        "version": version,
        "sha256": _canonical_digest(value["sha256"], name="component sha256"),
    }


def _load_composition(
    path: Path,
    *,
    source_sha: str,
    files: list[tuple[str, Path, bytes]],
) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError("Windows composition manifest is missing or invalid") from error
    if not isinstance(value, dict):
        raise BundleError("Windows composition manifest must be an object")

    required = {
        "schema_version",
        "product",
        "source_sha",
        "dependency_lock_sha256",
        "sbom_sha256",
        "schema_compatibility",
        "runtime",
        "components",
    }
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise BundleError(
            "Windows composition manifest has invalid fields"
            + (": " + "; ".join(detail) if detail else "")
        )

    if value["schema_version"] != "1.0.0":
        raise BundleError("unsupported Windows composition schema_version")
    if value["product"] != "AutoTrade":
        raise BundleError("Windows composition product must be AutoTrade")
    composition_sha = _required_text(value["source_sha"], name="composition source_sha").lower()
    if SOURCE_SHA.fullmatch(composition_sha) is None:
        raise BundleError("composition source_sha must be an exact 40-character Git SHA")
    if composition_sha != source_sha:
        raise BundleError("composition source_sha does not match bundle source_sha")

    schema_range = value["schema_compatibility"]
    if not isinstance(schema_range, dict) or set(schema_range) != {"minimum", "maximum"}:
        raise BundleError(
            "composition schema_compatibility must contain exactly minimum and maximum"
        )
    normalized_schema_range = {
        "minimum": _required_text(schema_range["minimum"], name="schema minimum"),
        "maximum": _required_text(schema_range["maximum"], name="schema maximum"),
    }

    runtime = value["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {
        "architecture",
        "runtime_identifier",
        "minimum_windows_version",
    }:
        raise BundleError(
            "composition runtime must contain architecture, runtime_identifier, "
            "minimum_windows_version"
        )
    normalized_runtime = {
        "architecture": _required_text(runtime["architecture"], name="runtime architecture"),
        "runtime_identifier": _required_text(
            runtime["runtime_identifier"], name="runtime identifier"
        ),
        "minimum_windows_version": _required_text(
            runtime["minimum_windows_version"], name="minimum Windows version"
        ),
    }
    expected_rid = {
        "x64": "win-x64",
        "arm64": "win-arm64",
    }.get(normalized_runtime["architecture"])
    if expected_rid is None:
        raise BundleError("composition runtime architecture must be x64 or arm64")
    if normalized_runtime["runtime_identifier"] != expected_rid:
        raise BundleError(
            "composition runtime_identifier does not match runtime architecture"
        )

    components_raw = value["components"]
    if not isinstance(components_raw, list) or not components_raw:
        raise BundleError("composition components must be a non-empty array")
    components = [
        _composition_component(item, index=index)
        for index, item in enumerate(components_raw)
    ]

    ids: set[str] = set()
    paths: set[str] = set()
    for component in components:
        component_id = component["component_id"]
        component_path = component["path"]
        if component_id in ids:
            raise BundleError(f"duplicate composition component_id: {component_id}")
        if component_path in paths:
            raise BundleError(f"duplicate composition component path: {component_path}")
        ids.add(component_id)
        paths.add(component_path)

    staged = {
        relative: "sha256:" + sha256(data).hexdigest()
        for relative, _, data in files
    }
    declared = {component["path"]: component["sha256"] for component in components}
    missing = sorted(set(staged) - set(declared))
    extra = sorted(set(declared) - set(staged))
    if missing or extra:
        detail = []
        if missing:
            detail.append("undeclared staging files=" + ",".join(missing))
        if extra:
            detail.append("declared files absent from staging=" + ",".join(extra))
        raise BundleError("composition/staging file set mismatch: " + "; ".join(detail))
    for relative, digest in staged.items():
        if declared[relative] != digest:
            raise BundleError(
                f"composition digest does not match staged file: {relative}"
            )

    dependency_lock_sha256 = _canonical_digest(
        value["dependency_lock_sha256"],
        name="dependency_lock_sha256",
    )
    sbom_sha256 = _canonical_digest(value["sbom_sha256"], name="sbom_sha256")
    by_kind: dict[str, list[dict[str, str]]] = {}
    for component in components:
        by_kind.setdefault(component["kind"], []).append(component)
    for kind, expected_digest in (
        ("dependency-lock", dependency_lock_sha256),
        ("sbom", sbom_sha256),
    ):
        matching = by_kind.get(kind, [])
        if len(matching) != 1:
            raise BundleError(
                f"composition requires exactly one {kind} component"
            )
        if matching[0]["sha256"] != expected_digest:
            raise BundleError(
                f"composition {kind} digest does not match declared component"
            )

    normalized = {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "source_sha": composition_sha,
        "dependency_lock_sha256": dependency_lock_sha256,
        "sbom_sha256": sbom_sha256,
        "schema_compatibility": normalized_schema_range,
        "runtime": normalized_runtime,
        "components": sorted(components, key=lambda item: item["path"]),
    }
    canonical = (
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return normalized, "sha256:" + sha256(canonical).hexdigest()


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits = 0
    archive.writestr(info, data)



def _prepare_atomic_destination(path: Path, *, name: str) -> Path:
    if path.is_symlink():
        raise BundleError(f"{name} cannot be a symlink")
    if path.exists() and not path.is_file():
        raise BundleError(f"{name} must be a regular file or absent")
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink():
        raise BundleError(f"{name} temporary path cannot be a symlink")
    if temporary.exists():
        if not temporary.is_file():
            raise BundleError(f"{name} temporary path must be a regular file or absent")
        temporary.unlink()
    return temporary


def _cleanup_temporary(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def build_bundle(
    *,
    staging: Path,
    output: Path,
    version: str,
    source_sha: str,
    mode: str,
    provenance_path: Path = DEFAULT_PROVENANCE,
    composition_path: Path | None = None,
) -> dict[str, object]:
    normalized_version = _required_text(version, name="version")
    normalized_sha = _required_text(source_sha, name="source_sha").lower()
    if SOURCE_SHA.fullmatch(normalized_sha) is None:
        raise BundleError("source_sha must be an exact 40-character lowercase Git SHA")
    if mode not in {"diagnostics", "release"}:
        raise BundleError("mode must be diagnostics or release")

    try:
        staging_resolved = staging.resolve(strict=True)
    except OSError as error:
        raise BundleError("staging must be an existing directory") from error
    output_resolved = output.resolve(strict=False)
    hash_resolved = output.with_suffix(output.suffix + ".sha256").resolve(strict=False)
    for candidate, name in ((output_resolved, "output"), (hash_resolved, "hash output")):
        try:
            candidate.relative_to(staging_resolved)
        except ValueError:
            pass
        else:
            raise BundleError(f"{name} must be outside the staging directory")

    provenance, provenance_sha256 = _load_provenance(provenance_path)
    blockers = provenance["blocking_issues"]
    if mode == "release" and provenance["release_eligible"] is not True:
        codes = [
            str(item.get("code", "UNKNOWN"))
            for item in blockers
            if isinstance(item, dict)
        ]
        raise BundleError(
            "release bundle is blocked by provenance gate: "
            + ", ".join(sorted(codes))
        )

    if mode == "release":
        provenance_source_sha = provenance.get("source_sha")
        if (
            not isinstance(provenance_source_sha, str)
            or SOURCE_SHA.fullmatch(provenance_source_sha.lower()) is None
        ):
            raise BundleError(
                "release provenance must bind an exact 40-character source_sha"
            )
        if provenance_source_sha.lower() != normalized_sha:
            raise BundleError(
                "release provenance source_sha does not match bundle source_sha"
            )

    files = _collect(staging)
    composition = None
    composition_sha256 = None
    if composition_path is not None:
        composition, composition_sha256 = _load_composition(
            composition_path,
            source_sha=normalized_sha,
            files=files,
        )
    elif mode == "release":
        raise BundleError(
            "release bundle requires an exact Windows composition manifest"
        )

    manifest = {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "version": normalized_version,
        "source_sha": normalized_sha,
        "mode": mode,
        "release_eligible": mode == "release" and provenance["release_eligible"] is True,
        "trading_authority_granted_by_artifact": False,
        "provenance_sha256": provenance_sha256,
        "composition_sha256": composition_sha256,
        "composition": composition,
        "provenance_blockers": [
            item.get("code", "UNKNOWN")
            for item in blockers
            if isinstance(item, dict)
        ],
        "files": [_entry_metadata(relative, data) for relative, _, data in files],
    }
    manifest_bytes = (
        json.dumps(
            manifest,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    hash_path = output.with_suffix(output.suffix + ".sha256")
    temporary = _prepare_atomic_destination(output, name="bundle output")
    hash_temporary = _prepare_atomic_destination(
        hash_path,
        name="bundle hash output",
    )
    try:
        try:
            with temporary.open("xb") as stream:
                with zipfile.ZipFile(stream, "w") as archive:
                    _write_entry(archive, "bundle-manifest.json", manifest_bytes)
                    for relative, _, data in files:
                        _write_entry(archive, f"payload/{relative}", data)
                stream.flush()
        except FileExistsError as error:
            raise BundleError(
                "bundle output temporary path changed before creation"
            ) from error

        digest = sha256(temporary.read_bytes()).hexdigest()
        digest_payload = f"{digest}  {output.name}\n".encode("utf-8")
        try:
            with hash_temporary.open("xb") as stream:
                stream.write(digest_payload)
                stream.flush()
        except FileExistsError as error:
            raise BundleError(
                "bundle hash output temporary path changed before creation"
            ) from error

        temporary.replace(output)
        hash_temporary.replace(hash_path)
    finally:
        _cleanup_temporary(temporary)
        _cleanup_temporary(hash_temporary)
    return {
        "output": str(output),
        "sha256": digest,
        "manifest": manifest,
        "hash_file": str(hash_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--mode", required=True, choices=("diagnostics", "release"))
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--composition", type=Path)
    args = parser.parse_args()
    try:
        result = build_bundle(
            staging=args.staging,
            output=args.output,
            version=args.version,
            source_sha=args.source_sha,
            mode=args.mode,
            provenance_path=args.provenance,
            composition_path=args.composition,
        )
    except BundleError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
