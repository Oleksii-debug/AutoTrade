"""Build an AutoTrade Velopack release from already-verified release evidence.

This tool is deliberately not a release trust root. It does not sign, upload,
apply, or authorize an update. It consumes the canonical release ZIP plus the
canonical installer-input manifest, materializes the exact verified payload into
an isolated temporary directory, invokes the repository-pinned Velopack CLI, and
publishes an explicitly unsigned artifact inventory for downstream WP-64 signing
and delivered-Windows qualification.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

from research.autotrade_research.artifacts.durable_publish import (
    DurablePublishLockError,
    atomic_write_bytes_with_sha256_sidecar,
    atomic_write_stream_with_sha256_sidecar,
)
from tools.build_windows_install_manifest import (
    InstallerManifestError,
    _assert_open_file_identity,
    _open_stable_regular_file,
    _sha256_stream,
    _verify_release_bundle_stream,
)


ROOT = Path(__file__).resolve().parents[1]
TOOL_MANIFEST = ROOT / ".config" / "dotnet-tools.json"
VELOPACK_VERSION = "1.2.158"
PACK_ID = "AutoTrade"
PACK_TITLE = "AutoTrade"
MAIN_EXE = "AutoTrade.Desktop.exe"
BUILD_MANIFEST_NAME = "autotrade-velopack-build.json"
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_INSTALLER_FIELDS = {
    "schema_version",
    "product",
    "source_sha",
    "version",
    "bundle_sha256",
    "release_provenance_sha256",
    "composition_sha256",
    "dependency_lock_sha256",
    "sbom_sha256",
    "schema_compatibility",
    "platform",
    "components",
    "target_framework",
    "runtime",
    "install_scope",
    "application_root_policy",
    "durable_state_policy",
    "update_policy",
    "fresh_install_policy",
    "files",
}


class VelopackPackagingError(ValueError):
    """Raised when a Velopack build cannot preserve AutoTrade release invariants."""


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VelopackPackagingError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _stable_bytes(path: Path, *, name: str) -> bytes:
    try:
        with _open_stable_regular_file(path, name=name) as stream:
            before = os.fstat(stream.fileno())
            payload = stream.read()
            after = _assert_open_file_identity(path, stream, name=name)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise VelopackPackagingError(f"{name} changed while being read")
            if len(payload) != after.st_size:
                raise VelopackPackagingError(f"{name} size changed while being read")
            return payload
    except InstallerManifestError as error:
        raise VelopackPackagingError(str(error)) from error


def _load_json_bytes(payload: bytes, *, name: str) -> dict[str, object]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
        )
    except UnicodeDecodeError as error:
        raise VelopackPackagingError(f"{name} must be UTF-8") from error
    except json.JSONDecodeError as error:
        raise VelopackPackagingError(f"{name} is invalid JSON") from error
    if not isinstance(value, dict):
        raise VelopackPackagingError(f"{name} must be a JSON object")
    return value


def _load_installer_manifest(path: Path) -> tuple[dict[str, object], str]:
    payload = _stable_bytes(path, name="installer input manifest")
    digest_hex = sha256(payload).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar_payload = _stable_bytes(sidecar, name="installer input digest")
    expected_sidecar = f"{digest_hex}  {path.name}\n".encode("utf-8")
    if sidecar_payload != expected_sidecar:
        raise VelopackPackagingError(
            "installer input digest does not match exact manifest bytes"
        )

    manifest = _load_json_bytes(payload, name="installer input manifest")
    if set(manifest) != EXPECTED_INSTALLER_FIELDS:
        raise VelopackPackagingError(
            "installer input manifest fields do not match supported schema"
        )
    if manifest.get("schema_version") != "1.0.0":
        raise VelopackPackagingError("unsupported installer input schema")
    if manifest.get("product") != "AutoTrade":
        raise VelopackPackagingError("installer input product identity is invalid")

    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or SOURCE_SHA.fullmatch(source_sha) is None:
        raise VelopackPackagingError("installer input source_sha is not canonical")
    version = manifest.get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise VelopackPackagingError(
            "Velopack release version must be canonical three-part SemVer without build metadata"
        )
    if manifest.get("target_framework") != "net10.0-windows":
        raise VelopackPackagingError(
            "Velopack packaging requires the qualified net10.0-windows target"
        )
    if manifest.get("install_scope") != "PER_USER":
        raise VelopackPackagingError("Velopack packaging requires PER_USER install scope")
    if (
        manifest.get("application_root_policy")
        != "LOCAL_APP_DATA_VERSIONED_APP_DIRECTORY"
    ):
        raise VelopackPackagingError(
            "installer input application root policy is not qualified for Velopack"
        )

    if manifest.get("durable_state_policy") != {
        "root": "LOCAL_APP_DATA_AUTOTRADE_STATE",
        "installer_may_mutate_state": False,
        "uninstall_default": "PRESERVE_DURABLE_STATE",
        "delete_state_requires_explicit_user_choice": True,
    }:
        raise VelopackPackagingError("installer durable-state policy is not canonical")
    if manifest.get("update_policy") != {
        "verified_windows_update_plan_required": True,
        "blind_retry_after_unknown_state_forbidden": True,
        "post_update_reconciliation_required": True,
    }:
        raise VelopackPackagingError("installer update policy is not canonical")
    if manifest.get("fresh_install_policy") != {
        "trading_authority_granted_by_installer": False,
        "qualification_required_before_financial_authority": True,
    }:
        raise VelopackPackagingError("installer fresh-install policy is not canonical")

    platform = manifest.get("platform")
    if not isinstance(platform, dict):
        raise VelopackPackagingError("installer platform must be an object")
    if platform.get("architecture") != "x64" or platform.get("runtime_identifier") != "win-x64":
        raise VelopackPackagingError(
            "first qualified Velopack path is restricted to win-x64"
        )
    minimum_windows = platform.get("minimum_windows_version")
    if not isinstance(minimum_windows, str) or not minimum_windows.strip():
        raise VelopackPackagingError("minimum Windows version is required")

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {"mode", "prerequisite"}:
        raise VelopackPackagingError("installer runtime policy is invalid")
    mode = runtime.get("mode")
    prerequisite = runtime.get("prerequisite")
    if mode == "SELF_CONTAINED":
        if prerequisite is not None:
            raise VelopackPackagingError(
                "self-contained runtime cannot declare a prerequisite"
            )
    elif mode == "FRAMEWORK_DEPENDENT":
        if (
            not isinstance(prerequisite, str)
            or not prerequisite
            or prerequisite != prerequisite.strip()
            or any(ord(character) < 0x20 for character in prerequisite)
        ):
            raise VelopackPackagingError(
                "framework-dependent runtime requires a canonical prerequisite"
            )
    else:
        raise VelopackPackagingError("unsupported installer runtime mode")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise VelopackPackagingError("installer input file inventory is empty")
    return manifest, "sha256:" + digest_hex


def _validate_tool_manifest(path: Path = TOOL_MANIFEST) -> None:
    value = _load_json_bytes(
        _stable_bytes(path, name="dotnet tool manifest"),
        name="dotnet tool manifest",
    )
    if value.get("version") != 1 or value.get("isRoot") is not True:
        raise VelopackPackagingError("dotnet tool manifest root/version is not canonical")
    tools = value.get("tools")
    if not isinstance(tools, dict):
        raise VelopackPackagingError("dotnet tool manifest tools are invalid")
    if tools.get("vpk") != {
        "version": VELOPACK_VERSION,
        "commands": ["vpk"],
    }:
        raise VelopackPackagingError(
            f"repository vpk tool must be pinned exactly to {VELOPACK_VERSION}"
        )


def _match_installer_to_bundle(
    installer: dict[str, object],
    verified: dict[str, object],
) -> None:
    composition = verified["composition"]
    pairs = (
        ("source_sha", verified["source_sha"]),
        ("version", verified["version"]),
        ("bundle_sha256", verified["bundle_sha256"]),
        ("release_provenance_sha256", verified["provenance_sha256"]),
        ("composition_sha256", verified["composition_sha256"]),
        ("dependency_lock_sha256", composition["dependency_lock_sha256"]),
        ("sbom_sha256", composition["sbom_sha256"]),
        ("schema_compatibility", composition["schema_compatibility"]),
        ("platform", composition["runtime"]),
        ("components", composition["components"]),
        ("files", verified["files"]),
    )
    for field, expected in pairs:
        if installer.get(field) != expected:
            raise VelopackPackagingError(
                f"installer input {field} does not match verified release bundle"
            )


def _safe_extract_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise VelopackPackagingError("verified payload path is unsafe")
    destination = root.joinpath(*pure.parts)
    try:
        destination.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise VelopackPackagingError("verified payload path escapes pack directory") from error
    return destination


def _verify_and_extract_bundle(
    bundle: Path,
    installer: dict[str, object],
    pack_dir: Path,
) -> dict[str, object]:
    try:
        with _open_stable_regular_file(bundle, name="release bundle") as stream:
            before = os.fstat(stream.fileno())
            bundle_digest = "sha256:" + _sha256_stream(stream)
            stream.seek(0)
            verified = _verify_release_bundle_stream(stream, bundle_digest)
            _match_installer_to_bundle(installer, verified)

            stream.seek(0)
            with zipfile.ZipFile(stream, "r") as archive:
                for item in verified["files"]:
                    source_relative = str(item["source_path"])
                    target_relative = str(item["target_relative_path"])
                    destination = _safe_extract_path(pack_dir, target_relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists() or destination.is_symlink():
                        raise VelopackPackagingError(
                            "verified payload extraction target already exists"
                        )
                    info = archive.getinfo(f"payload/{source_relative}")
                    digest = sha256()
                    observed_size = 0
                    with archive.open(info, "r") as source, destination.open("xb") as target:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            target.write(chunk)
                            digest.update(chunk)
                            observed_size += len(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    observed_digest = "sha256:" + digest.hexdigest()
                    if observed_digest != item["sha256"] or observed_size != item["size"]:
                        raise VelopackPackagingError(
                            f"extracted payload changed: {target_relative}"
                        )

            after = _assert_open_file_identity(
                bundle,
                stream,
                name="release bundle",
            )
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise VelopackPackagingError(
                    "release bundle changed during Velopack materialization"
                )
    except InstallerManifestError as error:
        raise VelopackPackagingError(str(error)) from error
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise VelopackPackagingError(
            "verified release payload could not be materialized"
        ) from error

    main_executable = pack_dir / MAIN_EXE
    if not main_executable.is_file() or main_executable.is_symlink():
        raise VelopackPackagingError(
            f"verified payload must contain root {MAIN_EXE}"
        )
    return verified


def _sanitized_vpk_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("VPK_")
    }
    environment["DOTNET_NOLOGO"] = "1"
    environment["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    return environment


def _vpk_command(
    *,
    pack_dir: Path,
    output_dir: Path,
    installer: dict[str, object],
) -> list[str]:
    command = [
        "dotnet",
        "tool",
        "run",
        "vpk",
        "--",
        "--skip-updates",
        "true",
        "--yes",
        "true",
        "pack",
        "--packId",
        PACK_ID,
        "--packVersion",
        str(installer["version"]),
        "--packDir",
        str(pack_dir),
        "--mainExe",
        MAIN_EXE,
        "--packTitle",
        PACK_TITLE,
        "--outputDir",
        str(output_dir),
        "--runtime",
        str(installer["platform"]["runtime_identifier"]),
        "--noPortable",
        "true",
        "--exclude",
        "(?!)",
        "--noDefaultExclude",
        "true",
    ]
    runtime = installer["runtime"]
    if runtime["mode"] == "FRAMEWORK_DEPENDENT":
        command.extend(["--framework", str(runtime["prerequisite"])])
    return command


def _validate_vpk_outputs(directory: Path, *, version: str) -> list[Path]:
    observed: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_symlink():
            raise VelopackPackagingError("Velopack emitted a symlink")
        if not path.is_file():
            raise VelopackPackagingError("Velopack emitted an unsupported filesystem entry")
        metadata = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise VelopackPackagingError(
                "Velopack output must be a regular file without hard-link aliases"
            )
        observed.append(path)

    names = {path.name for path in observed}
    if any("portable" in name.casefold() for name in names):
        raise VelopackPackagingError("portable Velopack output is forbidden")
    if any(name.casefold().endswith(".msi") for name in names):
        raise VelopackPackagingError("MSI output is outside the qualified per-user path")
    if any("-delta.nupkg" in name.casefold() for name in names):
        raise VelopackPackagingError("delta output is outside the first qualified path")

    full_packages = [
        name
        for name in names
        if name.startswith(f"{PACK_ID}-") and name.endswith("-full.nupkg")
    ]
    required = {
        f"{PACK_ID}-Setup.exe",
        "releases.win.json",
    }
    if not required <= names or len(full_packages) != 1:
        raise VelopackPackagingError(
            "Velopack output is missing the required Setup/full-package/release-index family"
        )
    if version not in full_packages[0]:
        raise VelopackPackagingError(
            "Velopack full-package filename does not bind the requested version"
        )

    allowed = required | {
        full_packages[0],
        "assets.win.json",
        "RELEASES",
    }
    unexpected = names - allowed
    if unexpected:
        raise VelopackPackagingError(
            "Velopack emitted unrecognized release artifacts: "
            + ", ".join(sorted(unexpected))
        )
    return observed


def _ensure_empty_output_directory(output_dir: Path) -> None:
    if output_dir.is_symlink():
        raise VelopackPackagingError("output directory must not be a symlink")
    if output_dir.exists() and not output_dir.is_dir():
        raise VelopackPackagingError("output directory must be a directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise VelopackPackagingError(
            "Velopack final output directory must be empty before publication"
        )


def _publish_file(source: Path, destination: Path) -> tuple[str, int]:
    digest_path = destination.with_suffix(destination.suffix + ".sha256")

    def writer(handle) -> None:
        with source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, handle, length=1024 * 1024)

    try:
        digest = atomic_write_stream_with_sha256_sidecar(
            destination,
            digest_path,
            writer,
        )
    except (DurablePublishLockError, OSError) as error:
        raise VelopackPackagingError(
            f"Velopack artifact publication failed closed: {destination.name}"
        ) from error
    return "sha256:" + digest, source.stat().st_size


def build_velopack_release(
    *,
    bundle: Path,
    installer_manifest: Path,
    output_dir: Path,
    runner=subprocess.run,
) -> dict[str, object]:
    """Build and publish an unsigned Velopack artifact family.

    The final build manifest is published last. Downstream release consumers must
    require that manifest and must independently apply WP-64 signing/trust plus
    delivered-Windows qualification before any release claim.
    """

    _ensure_empty_output_directory(output_dir)
    _validate_tool_manifest()
    installer, installer_digest = _load_installer_manifest(installer_manifest)

    with TemporaryDirectory(prefix="autotrade-velopack-pack-") as pack_root_raw, TemporaryDirectory(
        prefix="autotrade-velopack-output-"
    ) as vpk_output_raw:
        pack_root = Path(pack_root_raw)
        vpk_output = Path(vpk_output_raw)
        verified = _verify_and_extract_bundle(bundle, installer, pack_root)
        command = _vpk_command(
            pack_dir=pack_root,
            output_dir=vpk_output,
            installer=installer,
        )
        try:
            completed = runner(
                command,
                cwd=ROOT,
                env=_sanitized_vpk_environment(),
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise VelopackPackagingError("vpk pack execution failed closed") from error
        if completed.returncode != 0:
            raise VelopackPackagingError(
                f"vpk pack failed with exit code {completed.returncode}"
            )

        generated = _validate_vpk_outputs(
            vpk_output,
            version=str(installer["version"]),
        )
        artifacts: list[dict[str, object]] = []
        for source in generated:
            digest, size = _publish_file(source, output_dir / source.name)
            artifacts.append(
                {
                    "path": source.name,
                    "sha256": digest,
                    "size": size,
                }
            )

    artifacts.sort(key=lambda item: str(item["path"]))
    build_manifest = {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "installer_technology": {
            "name": "Velopack",
            "version": VELOPACK_VERSION,
            "pack_id": PACK_ID,
            "delivery_mode": "PER_USER_SETUP",
        },
        "source_sha": verified["source_sha"],
        "version": verified["version"],
        "release_bundle_sha256": verified["bundle_sha256"],
        "installer_input_sha256": installer_digest,
        "platform": installer["platform"],
        "artifacts": artifacts,
        "signing_status": "UNSIGNED_REQUIRES_WP64",
        "release_eligible": False,
        "trading_authority_granted_by_artifact": False,
    }
    payload = (
        json.dumps(
            build_manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    manifest_path = output_dir / BUILD_MANIFEST_NAME
    manifest_sidecar = manifest_path.with_suffix(manifest_path.suffix + ".sha256")
    try:
        manifest_digest = atomic_write_bytes_with_sha256_sidecar(
            manifest_path,
            manifest_sidecar,
            payload,
        )
    except (DurablePublishLockError, OSError) as error:
        raise VelopackPackagingError(
            "Velopack build-manifest publication failed closed"
        ) from error

    return {
        "output_dir": str(output_dir),
        "build_manifest": str(manifest_path),
        "build_manifest_sha256": "sha256:" + manifest_digest,
        "manifest": build_manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--installer-manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = build_velopack_release(
            bundle=args.bundle,
            installer_manifest=args.installer_manifest,
            output_dir=args.output_dir,
        )
    except VelopackPackagingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
