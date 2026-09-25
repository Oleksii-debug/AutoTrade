"""Verify an AutoTrade release bundle and emit deterministic installer inputs.

This tool does not create or sign an installer. It gives a future MSI/MSIX/WiX
layer one fail-closed, content-addressed inventory and explicit state/runtime
policies instead of trusting an arbitrary staging directory.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile


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


def _zip_member_is_regular(info: zipfile.ZipInfo) -> bool:
    if info.is_dir():
        return False
    mode = (info.external_attr >> 16) & 0o170000
    return mode in {0, 0o100000}


def verify_release_bundle(bundle: Path) -> dict[str, object]:
    if bundle.is_symlink() or not bundle.is_file():
        raise InstallerManifestError("release bundle must be a regular file")
    bundle_bytes = bundle.read_bytes()
    bundle_digest = "sha256:" + sha256(bundle_bytes).hexdigest()
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
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
                payload = archive.read(info)
                observed_digest = "sha256:" + sha256(payload).hexdigest()
                if observed_digest != digest:
                    raise InstallerManifestError(
                        f"bundle payload digest mismatch: {relative}"
                    )
                if len(payload) != size or info.file_size != size:
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

    return {
        "bundle_sha256": bundle_digest,
        "source_sha": source_sha,
        "version": version,
        "provenance_sha256": provenance_sha256,
        "files": sorted(
            verified_files,
            key=lambda item: str(item["target_relative_path"]),
        ),
    }


def build_installer_input_manifest(
    *,
    bundle: Path,
    output: Path,
    target_framework: str,
    runtime_mode: str,
    runtime_prerequisite: str | None = None,
) -> dict[str, object]:
    verified = verify_release_bundle(bundle)
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
    if output.exists() and output.is_dir():
        raise InstallerManifestError("installer manifest output cannot be a directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    manifest_digest = sha256(payload).hexdigest()
    digest_path = output.with_suffix(output.suffix + ".sha256")
    digest_path.write_text(
        f"{manifest_digest}  {output.name}\n",
        encoding="utf-8",
        newline="\n",
    )
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
