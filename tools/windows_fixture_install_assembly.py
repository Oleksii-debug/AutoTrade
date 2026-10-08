"""Provider-free Windows *fixture* install/update/rollback/uninstall rehearsal.

This is NOT a qualified MSI/MSIX, signed installer, production updater or
financial/Host execution path. All bytes must first pass the existing canonical
release-bundle validator. A synthetic release-eligible provenance fixture does
not prove trust: only isolated non-executing test roots are permitted.

Canonical authorities are REUSED:
* tools.build_windows_install_manifest (bundle, content, composition verifier)
* research.autotrade_research.artifacts.durable_publish (cross-process lock and
  fsynced atomic metadata publication)
* mvp.autotrade_mvp.windows_update (independently trusted release plan and
  pending UNKNOWN/reconciliation fence, never overridden here).
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from research.autotrade_research.artifacts.durable_publish import (
    atomic_write_json, durable_path_lock,
)
from tools.build_windows_install_manifest import (
    InstallerManifestError,
    _assert_open_file_identity,
    _open_stable_regular_file,
    _sha256_stream,
    _verify_release_bundle_stream,
    _safe_relative,
)


class FixtureAssemblyError(ValueError):
    pass


_ROOT = "autotrade-fixture-only"
_MARKER = "AUTOTRADE_NONEXECUTING_INSTALL_FIXTURE_v1"
_MANIFEST = "fixture-inventory.json"
_SELECT = "selected-fixture.json"


def _root(path: Path, *, fixture_only: bool) -> Path:
    if fixture_only is not True or type(path) is not type(Path()):
        raise FixtureAssemblyError("fixture_only=True and exact Path are mandatory")
    if not path.is_absolute() or path.name != _ROOT:
        raise FixtureAssemblyError("fixture root must be an absolute autotrade-fixture-only path")
    for node in (path, *path.parents):
        if node.is_symlink():
            raise FixtureAssemblyError("fixture root traverses a symlink")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_checked(path: Path, *, limit: int = 2_000_000) -> dict:
    if path.is_symlink():
        raise FixtureAssemblyError("fixture record must not be a symlink")
    try:
        with _open_stable_regular_file(path, name="fixture record") as held:
            before = os.fstat(held.fileno())
            if before.st_size > limit:
                raise FixtureAssemblyError("fixture record exceeds bounded envelope")
            content = held.read(limit + 1)
            after = _assert_open_file_identity(path, held, name="fixture record")
            if len(content) > limit or len(content) != before.st_size or (
                before.st_size, before.st_mtime_ns, before.st_ctime_ns
            ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise FixtureAssemblyError("fixture record changed during read")
        obj = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, ValueError, UnicodeError, InstallerManifestError) as error:
        raise FixtureAssemblyError("fixture record is unavailable or malformed") from error
    if type(obj) is not dict:
        raise FixtureAssemblyError("fixture record must be an object")
    return obj


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise FixtureAssemblyError("duplicate fixture record field")
        result[key] = value
    return result


def _version(verified: dict) -> str:
    if verified["source_sha"].isalnum() is False:
        raise FixtureAssemblyError("unexpected source SHA")
    return verified["source_sha"] + "-" + verified["bundle_sha256"][7:]


def _inventory(verified: dict, version: str) -> dict:
    return {
        "schema_version": 1,
        "evidence_class": "UNSIGNED_NONEXECUTING_FIXTURE",
        "source_sha": verified["source_sha"],
        "bundle_sha256": verified["bundle_sha256"],
        "version_directory": version,
        "files": verified["files"],
        "host_started": False,
        "trading_authority_granted": False,
        "independently_signed_release": False,
    }


def _check_inventory(root: Path, version: str) -> dict:
    if type(version) is not str or not version or "/" in version or "\\" in version:
        raise FixtureAssemblyError("fixture version label is invalid")
    if not all(c in "0123456789abcdef-" for c in version) or len(version) != 105:
        raise FixtureAssemblyError("fixture version label is not content-addressed")
    versions = root / "versions"
    product = versions / version
    if versions.is_symlink() or product.is_symlink() or not product.is_dir():
        raise FixtureAssemblyError("fixture installed version is unavailable")
    inventory = _read_checked(product / _MANIFEST)
    if (
        set(inventory) != {
            "schema_version", "evidence_class", "source_sha", "bundle_sha256",
            "version_directory", "files", "host_started",
            "trading_authority_granted", "independently_signed_release",
        }
        or inventory["schema_version"] != 1
        or inventory["evidence_class"] != "UNSIGNED_NONEXECUTING_FIXTURE"
        or inventory["version_directory"] != version
        or inventory["source_sha"] + "-" + inventory["bundle_sha256"][7:] != version
        or inventory["host_started"] is not False
        or inventory["trading_authority_granted"] is not False
        or inventory["independently_signed_release"] is not False
        or type(inventory["files"]) is not list
        or not inventory["files"]
    ):
        raise FixtureAssemblyError("fixture installed inventory is not canonical")
    seen = set()
    for item in inventory["files"]:
        if type(item) is not dict or set(item) != {
            "source_path", "target_relative_path", "sha256", "size",
        }:
            raise FixtureAssemblyError("fixture file inventory shape invalid")
        rel = _safe_relative(item["target_relative_path"])
        if rel != item["source_path"] or rel in seen:
            raise FixtureAssemblyError("fixture file inventory path invalid")
        seen.add(rel)
        f = product / "payload" / rel
        if any(p.is_symlink() for p in (f, *list(f.parents)[:len(Path(rel).parts)])):
            raise FixtureAssemblyError("fixture installed file path is a symlink")
        try:
            with _open_stable_regular_file(f, name="fixture payload") as held:
                before = os.fstat(held.fileno())
                digest = "sha256:" + _sha256_stream(held)
                after = _assert_open_file_identity(f, held, name="fixture payload")
                if (
                    before.st_size, before.st_mtime_ns, before.st_ctime_ns
                ) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise FixtureAssemblyError("installed fixture payload changed during read")
        except (OSError, InstallerManifestError) as error:
            raise FixtureAssemblyError("fixture file is not regular") from error
        if before.st_size != item["size"] or digest != item["sha256"]:
            raise FixtureAssemblyError("installed fixture payload differs from verified bytes")
    allowed = {str(Path("payload") / p) for p in seen} | {_MANIFEST}
    found = {f.relative_to(product).as_posix() for f in product.rglob("*") if f.is_file() or f.is_symlink()}
    if found != allowed:
        raise FixtureAssemblyError("fixture installation has untracked or missing files")
    return inventory


def _selection(root: Path) -> dict | None:
    path = root / _SELECT
    if not path.exists() and not path.is_symlink():
        return None
    selected = _read_checked(path)
    if set(selected) != {"schema_version", "mode", "version_directory", "host_started", "trading_authority_granted"} or (
        selected.get("schema_version") != 1
        or selected.get("mode") != "UNSIGNED_NONEXECUTING_FIXTURE"
        or selected.get("host_started") is not False
        or selected.get("trading_authority_granted") is not False
    ):
        raise FixtureAssemblyError("fixture selected version record is invalid")
    _check_inventory(root, selected["version_directory"])
    return selected


def _select(root: Path, version: str) -> None:
    _check_inventory(root, version)
    atomic_write_json(root / _SELECT, {
        "schema_version": 1,
        "mode": "UNSIGNED_NONEXECUTING_FIXTURE",
        "version_directory": version,
        "host_started": False,
        "trading_authority_granted": False,
    })


def fixture_install_bundle(
    bundle: Path,
    fixture_root: Path,
    *,
    fixture_only: bool,
    fail_after_files: int | None = None,
) -> dict:
    """Rehearse a verified content-addressed per-user install or update.

    The selected old version is never altered before complete candidate staging
    and verification. No executable is launched; no state or real release
    signing/financial/Host authority is created.
    """
    root = _root(fixture_root, fixture_only=fixture_only)
    if type(bundle) is not type(Path()) or not bundle.is_absolute():
        raise FixtureAssemblyError("release fixture archive must be an absolute Path")
    if fail_after_files is not None and (type(fail_after_files) is not int or fail_after_files < 0):
        raise FixtureAssemblyError("failure injection must be an exact nonnegative integer")
    versions = root / "versions"
    with durable_path_lock(root / "fixture-install-operation"):
        if versions.is_symlink():
            raise FixtureAssemblyError("version directory cannot be a symlink")
        versions.mkdir(parents=True, exist_ok=True)
        old = _selection(root)
        with _open_stable_regular_file(bundle, name="release bundle") as held:
            opened = os.fstat(held.fileno())
            source_hash = "sha256:" + _sha256_stream(held)
            held.seek(0)
            verified = _verify_release_bundle_stream(held, source_hash)
            version = _version(verified)
            dest = versions / version
            if dest.exists() or dest.is_symlink():
                raise FixtureAssemblyError("fixture version already installed; no silent overwrite")
            temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=versions))
            try:
                with zipfile.ZipFile(held, "r") as archive:
                    for index, item in enumerate(verified["files"]):
                        if fail_after_files is not None and index >= fail_after_files:
                            raise OSError("simulated staging disk full")
                        relative = _safe_relative(item["target_relative_path"])
                        target = temporary / "payload" / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open("payload/" + relative) as reader, target.open("xb") as writer:
                            digest = sha256()
                            size = 0
                            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                                digest.update(chunk)
                                size += len(chunk)
                                writer.write(chunk)
                            writer.flush()
                            os.fsync(writer.fileno())
                        if size != item["size"] or "sha256:" + digest.hexdigest() != item["sha256"]:
                            raise FixtureAssemblyError("fixture source payload changed during extraction")
                _assert_open_file_identity(bundle, held, name="release bundle")
                after = os.fstat(held.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise FixtureAssemblyError("release fixture archive changed during extraction")
                # Staging is invisible until rename, so a single fsynced
                # exclusive file avoids introducing a permanent publication
                # lock sidecar into the immutable payload inventory.
                with (temporary / _MANIFEST).open("xb") as receipt:
                    receipt.write((json.dumps(_inventory(verified, version),
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                        + "\n").encode("utf-8"))
                    receipt.flush()
                    os.fsync(receipt.fileno())
                # Stage is invisible to selection until the complete verified
                # version is moved atomically to its immutable content ID.
                temporary.rename(dest)
                temporary = None
                _check_inventory(root, version)
                _select(root, version)
                return {
                    "disposition": "FIXTURE_STAGED_AND_SELECTED",
                    "source_sha": verified["source_sha"],
                    "version_directory": version,
                    "replaced_version": old["version_directory"] if old else None,
                    "host_started": False,
                    "trading_authority_granted": False,
                    "release_qualified": False,
                }
            finally:
                if temporary is not None and temporary.exists():
                    shutil.rmtree(temporary)


def fixture_rollback_version(fixture_root: Path, version: str, *, fixture_only: bool) -> dict:
    root = _root(fixture_root, fixture_only=fixture_only)
    with durable_path_lock(root / "fixture-install-operation"):
        selected = _selection(root)
        if selected is None:
            raise FixtureAssemblyError("no selected installed fixture to roll back")
        if selected["version_directory"] == version:
            raise FixtureAssemblyError("rollback must select a distinct prior version")
        _select(root, version)
        return {"disposition": "FIXTURE_VERSION_SELECTION_ONLY", "version_directory": version,
                "host_started": False, "trading_authority_granted": False}


def fixture_uninstall(fixture_root: Path, *, fixture_only: bool) -> dict:
    """Remove ONLY isolated fixture program binaries; preserve user data."""
    root = _root(fixture_root, fixture_only=fixture_only)
    with durable_path_lock(root / "fixture-install-operation"):
        selected = _selection(root)
        versions = root / "versions"
        if versions.is_symlink():
            raise FixtureAssemblyError("version root is a symlink")
        for version in (versions.iterdir() if versions.exists() else []):
            if version.is_symlink() or not version.is_dir() or version.name.startswith("."):
                raise FixtureAssemblyError("untracked fixture version directory blocks uninstall")
            _check_inventory(root, version.name)
        if selected is not None:
            (root / _SELECT).unlink()
        # Durable state is at root/state and NEVER traversed, reset or touched.
        for version in (versions.iterdir() if versions.exists() else []):
            shutil.rmtree(version)
        return {"disposition": "FIXTURE_PROGRAM_REMOVED", "durable_state_preserved": True,
                "host_started": False, "trading_authority_granted": False}


def main() -> int:
    """Keyboard/terminal-only TEST interface; no production installer mode."""
    import argparse
    parser = argparse.ArgumentParser(description=(
        "AutoTrade NONEXECUTING installation fixture. "
        "Never runs Host or authorizes real trading. Not a signed installer."
    ))
    parser.add_argument("--root", required=True, type=Path,
                        help="Absolute existing or new directory named autotrade-fixture-only")
    parser.add_argument("--fixture-only", action="store_true", required=True,
                        help="Required acknowledgment of nonexecuting fixture mode")
    action = parser.add_subparsers(dest="action", required=True)
    install = action.add_parser("install", help="Clean fixture install or side-by-side update")
    install.add_argument("--bundle", required=True, type=Path)
    rollback = action.add_parser("rollback", help="Select a prior verified fixture version")
    rollback.add_argument("--version", required=True)
    action.add_parser("uninstall", help="Delete only fixture program files; preserve state")
    args = parser.parse_args()
    try:
        if args.action == "install":
            result = fixture_install_bundle(
                args.bundle, args.root, fixture_only=args.fixture_only
            )
        elif args.action == "rollback":
            result = fixture_rollback_version(
                args.root, args.version, fixture_only=args.fixture_only
            )
        else:
            result = fixture_uninstall(args.root, fixture_only=args.fixture_only)
    except (OSError, InstallerManifestError, FixtureAssemblyError) as error:
        parser.exit(status=2, message="AutoTrade fixture FAIL-CLOSED: " + str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
