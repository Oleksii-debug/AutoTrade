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


def _is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in FORBIDDEN_BASENAMES:
        return True
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    lowered_parts = {part.lower() for part in path.parts}
    return bool(lowered_parts & {"secrets", "credentials", "private-keys"})


def _collect(staging: Path) -> list[tuple[str, Path, bytes]]:
    if not staging.is_dir():
        raise BundleError("staging must be an existing directory")
    collected: list[tuple[str, Path, bytes]] = []
    for path in sorted(staging.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise BundleError(f"symlinks are forbidden in bundles: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleError(f"unsupported filesystem entry: {path}")
        relative = _safe_relative(path, staging)
        if _is_sensitive(PurePosixPath(relative)):
            raise BundleError(f"sensitive path is forbidden in bundles: {relative}")
        collected.append((relative, path, path.read_bytes()))
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


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.flag_bits = 0
    archive.writestr(info, data)


def build_bundle(
    *,
    staging: Path,
    output: Path,
    version: str,
    source_sha: str,
    mode: str,
    provenance_path: Path = DEFAULT_PROVENANCE,
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
    manifest = {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "version": normalized_version,
        "source_sha": normalized_sha,
        "mode": mode,
        "release_eligible": mode == "release" and provenance["release_eligible"] is True,
        "trading_authority_granted_by_artifact": False,
        "provenance_sha256": provenance_sha256,
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
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            _write_entry(archive, "bundle-manifest.json", manifest_bytes)
            for relative, _, data in files:
                _write_entry(archive, f"payload/{relative}", data)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()

    digest = sha256(output.read_bytes()).hexdigest()
    hash_path = output.with_suffix(output.suffix + ".sha256")
    hash_path.write_text(
        f"{digest}  {output.name}\n",
        encoding="utf-8",
        newline="\n",
    )
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
    args = parser.parse_args()
    try:
        result = build_bundle(
            staging=args.staging,
            output=args.output,
            version=args.version,
            source_sha=args.source_sha,
            mode=args.mode,
            provenance_path=args.provenance,
        )
    except BundleError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
