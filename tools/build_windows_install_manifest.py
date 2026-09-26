"""Verify an AutoTrade release bundle and emit deterministic installer inputs.

This tool does not create or sign an installer. It gives a future MSI/MSIX/WiX
layer one fail-closed, content-addressed inventory and explicit state/runtime
policies instead of trusting an arbitrary staging directory.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
import os
import stat
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

from research.autotrade_research.artifacts.durable_publish import (
    DurablePublishLockError,
    atomic_write_bytes,
    durable_path_lock,
    validate_publication_destination,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_WINDOWS_RESERVED_BASENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class InstallerManifestError(ValueError):
    pass


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstallerManifestError(f"{name} is required")
    return value.strip()


def _safe_relative(value: object) -> str:
    raw = _text(value, name="bundle file path")
    if "\\" in raw:
        raise InstallerManifestError(
            "bundle file path contains a Windows separator and is unsafe for Windows"
        )
    if any(char in raw for char in ':*?"<>|') or any(
        ord(char) < 32 for char in raw
    ):
        raise InstallerManifestError(
            "bundle file path contains a Windows-forbidden character"
        )
    if "//" in raw:
        raise InstallerManifestError("bundle file path is unsafe and noncanonical")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise InstallerManifestError("bundle file path is unsafe")
    for part in path.parts:
        if part.endswith((" ", ".")):
            raise InstallerManifestError(
                "bundle file path has a trailing space or dot and is unsafe for Windows"
            )
        basename = part.split(".", 1)[0].upper()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            raise InstallerManifestError(
                "bundle file path uses a reserved Windows name "
                "(reserved Windows device)"
            )
    return path.as_posix()


def _windows_path_key(relative: str) -> str:
    return relative.casefold()


def _digest(value: object, *, name: str) -> str:
    raw = _text(value, name=name)
    if not raw.startswith("sha256:") or _SHA256.fullmatch(raw[7:]) is None:
        raise InstallerManifestError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return raw


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _verify_composition(
    value: object,
    *,
    source_sha: str,
    verified_files: list[dict[str, object]],
) -> dict[str, object]:
    """Independently bind installer inputs to the exact release composition."""

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
    if not isinstance(value, dict) or set(value) != required:
        raise InstallerManifestError(
            "release bundle composition structure is not canonical"
        )
    if value.get("schema_version") != "1.0.0":
        raise InstallerManifestError(
            "unsupported release bundle composition schema"
        )
    if value.get("product") != "AutoTrade" or value.get("source_sha") != source_sha:
        raise InstallerManifestError(
            "release bundle composition identity does not match bundle"
        )

    dependency_lock = _digest(
        value.get("dependency_lock_sha256"),
        name="composition dependency_lock_sha256",
    )
    sbom = _digest(
        value.get("sbom_sha256"),
        name="composition sbom_sha256",
    )
    schema = value.get("schema_compatibility")
    if not isinstance(schema, dict) or set(schema) != {"minimum", "maximum"}:
        raise InstallerManifestError(
            "composition schema_compatibility must contain minimum and maximum"
        )
    normalized_schema = {
        "minimum": _text(schema["minimum"], name="schema minimum"),
        "maximum": _text(schema["maximum"], name="schema maximum"),
    }

    runtime = value.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {
        "architecture",
        "runtime_identifier",
        "minimum_windows_version",
    }:
        raise InstallerManifestError("composition runtime structure is invalid")
    architecture = _text(runtime["architecture"], name="runtime architecture")
    expected_rid = {"x64": "win-x64", "arm64": "win-arm64"}.get(architecture)
    if expected_rid is None:
        raise InstallerManifestError(
            "composition runtime architecture must be x64 or arm64"
        )
    runtime_identifier = _text(
        runtime["runtime_identifier"], name="runtime identifier"
    )
    if runtime_identifier != expected_rid:
        raise InstallerManifestError(
            "composition runtime_identifier does not match architecture"
        )
    minimum_windows = _text(
        runtime["minimum_windows_version"],
        name="minimum Windows version",
    )
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", minimum_windows) is None:
        raise InstallerManifestError(
            "minimum Windows version must use major.minor.build"
        )
    normalized_runtime = {
        "architecture": architecture,
        "runtime_identifier": runtime_identifier,
        "minimum_windows_version": minimum_windows,
    }

    raw_components = value.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise InstallerManifestError("composition components must be non-empty")
    components: list[dict[str, str]] = []
    ids: set[str] = set()
    paths: set[str] = set()
    windows_paths: set[str] = set()
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, dict) or set(raw) != {
            "component_id",
            "kind",
            "path",
            "version",
            "sha256",
        }:
            raise InstallerManifestError(
                f"composition components[{index}] structure is invalid"
            )
        component_id = _text(raw["component_id"], name="component_id")
        kind = _text(raw["kind"], name="component kind")
        relative = _safe_relative(raw["path"])
        version = _text(raw["version"], name="component version")
        digest = _digest(raw["sha256"], name="component sha256")
        windows_key = _windows_path_key(relative)
        if component_id in ids:
            raise InstallerManifestError("duplicate composition component_id")
        if relative in paths or windows_key in windows_paths:
            raise InstallerManifestError(
                "duplicate or Windows-colliding composition component path"
            )
        ids.add(component_id)
        paths.add(relative)
        windows_paths.add(windows_key)
        components.append(
            {
                "component_id": component_id,
                "kind": kind,
                "path": relative,
                "version": version,
                "sha256": digest,
            }
        )

    file_digests = {
        str(item["target_relative_path"]): str(item["sha256"])
        for item in verified_files
    }
    component_digests = {item["path"]: item["sha256"] for item in components}
    if component_digests != file_digests:
        raise InstallerManifestError(
            "composition component inventory does not match verified bundle payload"
        )

    by_kind: dict[str, list[dict[str, str]]] = {}
    for item in components:
        by_kind.setdefault(item["kind"], []).append(item)
    for kind, expected in (
        ("dependency-lock", dependency_lock),
        ("sbom", sbom),
    ):
        matches = by_kind.get(kind, [])
        if len(matches) != 1 or matches[0]["sha256"] != expected:
            raise InstallerManifestError(
                f"composition {kind} identity does not match component inventory"
            )

    return {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "source_sha": source_sha,
        "dependency_lock_sha256": dependency_lock,
        "sbom_sha256": sbom,
        "schema_compatibility": normalized_schema,
        "runtime": normalized_runtime,
        "components": sorted(components, key=lambda item: item["path"]),
    }


def _open_stable_regular_file(path: Path, *, name: str):
    """Open one immutable-by-identity verification snapshot without path re-open."""

    try:
        stream = path.open("rb")
    except OSError as error:
        raise InstallerManifestError(f"{name} must be an existing regular file") from error
    try:
        _assert_open_file_identity(path, stream, name=name)
    except BaseException:
        stream.close()
        raise
    return stream


def _stable_file_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    """Return metadata that must remain stable for one verified open file."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _assert_open_file_identity(
    path: Path,
    stream,
    *,
    name: str,
    expected: os.stat_result | None = None,
) -> os.stat_result:
    try:
        opened = os.fstat(stream.fileno())
        current = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise InstallerManifestError(
            f"{name} identity cannot be verified"
        ) from error
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        raise InstallerManifestError(f"{name} must be a regular non-symlink file")
    if opened.st_nlink != 1 or current.st_nlink != 1:
        raise InstallerManifestError(f"{name} must not have hard-link aliases")
    opened_fingerprint = _stable_file_fingerprint(opened)
    current_fingerprint = _stable_file_fingerprint(current)
    if opened_fingerprint != current_fingerprint:
        raise InstallerManifestError(f"{name} changed during verification")
    if (
        expected is not None
        and opened_fingerprint != _stable_file_fingerprint(expected)
    ):
        raise InstallerManifestError(f"{name} changed during verification")
    return opened


def _sha256_stream(stream) -> str:
    digest = sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[str, int]:
    digest = sha256()
    observed_size = 0
    with archive.open(info, "r") as member:
        for chunk in iter(lambda: member.read(1024 * 1024), b""):
            observed_size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), observed_size


def _zip_member_is_regular(info: zipfile.ZipInfo) -> bool:
    if info.is_dir():
        return False
    mode = (info.external_attr >> 16) & 0o170000
    return mode in {0, 0o100000}


def verify_release_bundle(bundle: Path) -> dict[str, object]:
    with _open_stable_regular_file(bundle, name="release bundle") as bundle_stream:
        before = _assert_open_file_identity(
            bundle,
            bundle_stream,
            name="release bundle",
        )
        bundle_digest = "sha256:" + _sha256_stream(bundle_stream)
        bundle_stream.seek(0)
        verified = _verify_release_bundle_stream(bundle_stream, bundle_digest)
        bundle_stream.seek(0)
        final_digest = "sha256:" + _sha256_stream(bundle_stream)
        if final_digest != bundle_digest:
            raise InstallerManifestError(
                "release bundle changed during verification"
            )
        _assert_open_file_identity(
            bundle,
            bundle_stream,
            name="release bundle",
            expected=before,
        )
        return verified


def _verify_release_bundle_stream(
    bundle_stream,
    bundle_digest: str,
) -> dict[str, object]:
    try:
        with zipfile.ZipFile(bundle_stream, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise InstallerManifestError(
                    "release bundle contains duplicate archive paths"
                )
            if "bundle-manifest.json" not in names:
                raise InstallerManifestError("bundle manifest is missing")
            manifest_info = archive.getinfo("bundle-manifest.json")
            if not _zip_member_is_regular(manifest_info):
                raise InstallerManifestError("bundle manifest is not a regular file")
            if manifest_info.compress_type != zipfile.ZIP_STORED:
                raise InstallerManifestError(
                    "bundle manifest compression is not canonical"
                )
            try:
                manifest = json.loads(
                    archive.read("bundle-manifest.json").decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InstallerManifestError("bundle manifest is invalid") from error
            if not isinstance(manifest, dict):
                raise InstallerManifestError("bundle manifest must be an object")
            expected_manifest_fields = {
                "schema_version",
                "product",
                "version",
                "source_sha",
                "mode",
                "release_eligible",
                "trading_authority_granted_by_artifact",
                "provenance_sha256",
                "composition_sha256",
                "composition",
                "provenance_blockers",
                "files",
            }
            if set(manifest) != expected_manifest_fields:
                raise InstallerManifestError(
                    "bundle manifest fields do not match supported schema"
                )
            if manifest.get("schema_version") != "1.0.0":
                raise InstallerManifestError(
                    "unsupported bundle manifest schema version"
                )
            if manifest.get("product") != "AutoTrade":
                raise InstallerManifestError("bundle product identity is invalid")
            if manifest.get("mode") != "release":
                raise InstallerManifestError(
                    "installer inputs require a release-mode bundle"
                )
            if manifest.get("release_eligible") is not True:
                raise InstallerManifestError("bundle is not release eligible")
            if manifest.get("trading_authority_granted_by_artifact") is not False:
                raise InstallerManifestError(
                    "bundle artifact must not grant trading authority"
                )
            blockers = manifest.get("provenance_blockers")
            if blockers != []:
                raise InstallerManifestError(
                    "release bundle still contains provenance blockers"
                )
            source_sha = _text(manifest.get("source_sha"), name="source_sha")
            if _GIT_SHA.fullmatch(source_sha) is None:
                raise InstallerManifestError("source_sha is not canonical")
            version = _text(manifest.get("version"), name="version")
            provenance_sha256 = _digest(
                manifest.get("provenance_sha256"),
                name="provenance_sha256",
            )
            composition_sha256 = _digest(
                manifest.get("composition_sha256"),
                name="composition_sha256",
            )
            composition = manifest.get("composition")
            if not isinstance(composition, dict):
                raise InstallerManifestError(
                    "release bundle composition evidence is missing"
                )
            if composition.get("product") != "AutoTrade":
                raise InstallerManifestError(
                    "release bundle composition product identity is invalid"
                )
            if composition.get("source_sha") != source_sha:
                raise InstallerManifestError(
                    "release bundle composition source_sha does not match bundle"
                )
            files = manifest.get("files")
            if not isinstance(files, list) or not files:
                raise InstallerManifestError("bundle file inventory is empty")

            verified_files: list[dict[str, object]] = []
            expected_payload_names: set[str] = set()
            seen_paths: set[str] = set()
            seen_windows_paths: set[str] = set()
            for item in files:
                if not isinstance(item, dict) or set(item) != {
                    "path",
                    "sha256",
                    "size",
                }:
                    raise InstallerManifestError(
                        "bundle file inventory entry is invalid"
                    )
                relative = _safe_relative(item["path"])
                if relative in seen_paths:
                    raise InstallerManifestError(
                        "bundle manifest contains duplicate paths"
                    )
                windows_key = _windows_path_key(relative)
                if windows_key in seen_windows_paths:
                    raise InstallerManifestError(
                        "bundle manifest contains Windows-colliding paths"
                    )
                seen_paths.add(relative)
                seen_windows_paths.add(windows_key)
                digest = _digest(item["sha256"], name="file sha256")
                size = item["size"]
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise InstallerManifestError("bundle file size is invalid")
                archive_name = f"payload/{relative}"
                expected_payload_names.add(archive_name)
                try:
                    info = archive.getinfo(archive_name)
                except KeyError as error:
                    raise InstallerManifestError(
                        f"bundle payload is missing: {relative}"
                    ) from error
                if not _zip_member_is_regular(info):
                    raise InstallerManifestError(
                        f"bundle payload is not a regular file: {relative}"
                    )
                if info.compress_type != zipfile.ZIP_STORED:
                    raise InstallerManifestError(
                        f"bundle payload compression is not canonical: {relative}"
                    )
                if info.file_size != size:
                    raise InstallerManifestError(
                        f"bundle payload size mismatch: {relative}"
                    )
                observed_hash, observed_size = _sha256_zip_member(archive, info)
                observed_digest = "sha256:" + observed_hash
                if observed_digest != digest:
                    raise InstallerManifestError(
                        f"bundle payload digest mismatch: {relative}"
                    )
                if observed_size != size:
                    raise InstallerManifestError(
                        f"bundle payload size mismatch: {relative}"
                    )
                verified_files.append(
                    {
                        "source_path": relative,
                        "target_relative_path": relative,
                        "sha256": digest,
                        "size": size,
                    }
                )

            observed_payload_names = {
                name for name in names if name.startswith("payload/")
            }
            if observed_payload_names != expected_payload_names:
                raise InstallerManifestError(
                    "bundle contains untracked or missing payload entries"
                )
            allowed_names = {"bundle-manifest.json"} | expected_payload_names
            if set(names) != allowed_names:
                raise InstallerManifestError(
                    "bundle contains untracked non-payload entries"
                )
    except zipfile.BadZipFile as error:
        raise InstallerManifestError("release bundle is not a valid ZIP") from error

    verified_files = sorted(
        verified_files,
        key=lambda item: str(item["target_relative_path"]),
    )
    verified_composition = _verify_composition(
        composition,
        source_sha=source_sha,
        verified_files=verified_files,
    )
    observed_composition_sha256 = (
        "sha256:" + sha256(_canonical_bytes(verified_composition)).hexdigest()
    )
    if observed_composition_sha256 != composition_sha256:
        raise InstallerManifestError(
            "composition_sha256 does not match canonical stored composition"
        )
    return {
        "bundle_sha256": bundle_digest,
        "source_sha": source_sha,
        "version": version,
        "provenance_sha256": provenance_sha256,
        "composition_sha256": composition_sha256,
        "composition": verified_composition,
        "files": verified_files,
    }



def _validate_output_destination(path: Path, *, name: str) -> None:
    try:
        validate_publication_destination(path)
    except (DurablePublishLockError, OSError) as error:
        raise InstallerManifestError(f"{name} is unsafe: {error}") from error


def _cleanup_legacy_temporary(path: Path) -> None:
    try:
        validate_publication_destination(path)
    except (DurablePublishLockError, OSError):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def build_installer_input_manifest(
    *,
    bundle: Path,
    output: Path,
    target_framework: str,
    runtime_mode: str,
    runtime_prerequisite: str | None = None,
) -> dict[str, object]:
    verified = verify_release_bundle(bundle)
    bundle_resolved = bundle.resolve(strict=False)
    output_resolved = output.resolve(strict=False)
    digest_path = output.with_suffix(output.suffix + ".sha256")
    digest_resolved = digest_path.resolve(strict=False)
    if output_resolved == bundle_resolved:
        raise InstallerManifestError(
            "installer manifest output must not overwrite verified release bundle"
        )
    if digest_resolved == bundle_resolved:
        raise InstallerManifestError(
            "installer manifest digest output must not overwrite verified release bundle"
        )

    framework = _text(target_framework, name="target_framework")
    mode = _text(runtime_mode, name="runtime_mode").upper()
    if mode not in {"SELF_CONTAINED", "FRAMEWORK_DEPENDENT"}:
        raise InstallerManifestError(
            "runtime_mode must be SELF_CONTAINED or FRAMEWORK_DEPENDENT"
        )
    prerequisite = None
    if mode == "FRAMEWORK_DEPENDENT":
        prerequisite = _text(
            runtime_prerequisite,
            name="runtime_prerequisite",
        )
    elif runtime_prerequisite is not None:
        raise InstallerManifestError(
            "self-contained runtime cannot claim external runtime prerequisite"
        )

    manifest = {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "source_sha": verified["source_sha"],
        "version": verified["version"],
        "bundle_sha256": verified["bundle_sha256"],
        "release_provenance_sha256": verified["provenance_sha256"],
        "composition_sha256": verified["composition_sha256"],
        "dependency_lock_sha256": verified["composition"]["dependency_lock_sha256"],
        "sbom_sha256": verified["composition"]["sbom_sha256"],
        "schema_compatibility": verified["composition"]["schema_compatibility"],
        "platform": verified["composition"]["runtime"],
        "components": verified["composition"]["components"],
        "target_framework": framework,
        "runtime": {
            "mode": mode,
            "prerequisite": prerequisite,
        },
        "install_scope": "PER_USER",
        "application_root_policy": "LOCAL_APP_DATA_VERSIONED_APP_DIRECTORY",
        "durable_state_policy": {
            "root": "LOCAL_APP_DATA_AUTOTRADE_STATE",
            "installer_may_mutate_state": False,
            "uninstall_default": "PRESERVE_DURABLE_STATE",
            "delete_state_requires_explicit_user_choice": True,
        },
        "update_policy": {
            "verified_windows_update_plan_required": True,
            "blind_retry_after_unknown_state_forbidden": True,
            "post_update_reconciliation_required": True,
        },
        "fresh_install_policy": {
            "trading_authority_granted_by_installer": False,
            "qualification_required_before_financial_authority": True,
        },
        "files": verified["files"],
    }
    payload = _canonical_bytes(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_output_destination(output, name="installer manifest output")
    _validate_output_destination(
        digest_path,
        name="installer manifest digest output",
    )
    _cleanup_legacy_temporary(output.with_name(output.name + ".tmp"))
    _cleanup_legacy_temporary(
        digest_path.with_name(digest_path.name + ".tmp")
    )
    manifest_digest = sha256(payload).hexdigest()
    digest_payload = f"{manifest_digest}  {output.name}\n".encode("utf-8")

    lock_paths = sorted(
        (output, digest_path),
        key=lambda path: str(path.resolve(strict=False)),
    )
    try:
        with ExitStack() as stack:
            for path in lock_paths:
                stack.enter_context(durable_path_lock(path))
            atomic_write_bytes(output, payload)
            atomic_write_bytes(digest_path, digest_payload)
    except (DurablePublishLockError, OSError) as error:
        raise InstallerManifestError(
            f"installer manifest publication failed closed: {error}"
        ) from error
    return {
        "output": str(output),
        "sha256": manifest_digest,
        "manifest": manifest,
        "hash_file": str(digest_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-framework", required=True)
    parser.add_argument(
        "--runtime-mode",
        required=True,
        choices=("SELF_CONTAINED", "FRAMEWORK_DEPENDENT"),
    )
    parser.add_argument("--runtime-prerequisite")
    args = parser.parse_args()
    try:
        result = build_installer_input_manifest(
            bundle=args.bundle,
            output=args.output,
            target_framework=args.target_framework,
            runtime_mode=args.runtime_mode,
            runtime_prerequisite=args.runtime_prerequisite,
        )
    except InstallerManifestError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
