#!/usr/bin/env python3
"""Prepare the exact approved LEAN checkout for AutoTrade qualification.

This is a deterministic composition step, not an upstream-source claim. It refuses
unknown inputs, replaces only the known vulnerable DotNetZip dependency with the
API-compatible ProDotNetZip fork, pins the patched System.Drawing.Common version,
and emits a hash manifest for the resulting composition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

DOTNETZIP_OLD = '<PackageReference Include="DotNetZip" Version="1.16.0" />'
DOTNETZIP_NEW = '<PackageReference Include="ProDotNetZip" Version="1.20.0" />'
DRAWING_REF = '<PackageReference Include="System.Drawing.Common" Version="4.7.2" />'
DRAWING_ANCHOR = '<PackageReference Include="QLNet" Version="1.13.1" />'

FILES = {
    "Compression/QuantConnect.Compression.csproj": (DOTNETZIP_OLD, DOTNETZIP_NEW),
    "Common/QuantConnect.csproj": (DRAWING_ANCHOR, DRAWING_ANCHOR + "\n    " + DRAWING_REF),
}

# Git blob identities at approved LEAN commit 985ef30ad3ac774218c5ac516b4cb0aa2655730f.
# Binding the composition to exact preimages prevents an anchor-preserving upstream drift
# from being silently treated as the approved source composition.
EXPECTED_UPSTREAM_BLOBS = {
    "Compression/QuantConnect.Compression.csproj": "66a19b888f51364414ef26523e57ecff41c1e7c6",
    "Common/QuantConnect.csproj": "f6d1c4c74b26e2df0de098310aceed2cdfc40a6f",
}


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def git_blob_digest(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _canonical_preimage(root: Path, relative: str, path: Path) -> tuple[bytes, str]:
    """Return the committed Git bytes when available, independent of checkout EOL filters."""
    working_bytes = path.read_bytes()
    git_marker = root / ".git"
    if not git_marker.exists():
        return working_bytes, git_blob_digest(working_bytes)

    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--quiet", "--", relative],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if diff.returncode == 1:
        raise ValueError(f"{relative}: working tree differs from approved Git preimage")
    if diff.returncode != 0:
        raise ValueError(f"{relative}: unable to verify clean approved Git preimage")

    try:
        actual_blob = subprocess.run(
            ["git", "-C", str(root), "rev-parse", f"HEAD:{relative}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        ).stdout.strip()
        committed_bytes = subprocess.run(
            ["git", "-C", str(root), "show", f"HEAD:{relative}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise ValueError(f"{relative}: unable to read approved Git preimage") from error

    if git_blob_digest(committed_bytes) != actual_blob:
        raise ValueError(f"{relative}: committed Git preimage failed blob self-check")
    return committed_bytes, actual_blob


def _replace_exact(path: Path, before: bytes, old: str, new: str) -> tuple[str, str]:
    text = before.decode("utf-8")
    if text.count(old) != 1:
        raise ValueError(f"{path}: expected exactly one approved composition anchor")
    if new in text and old != new:
        raise ValueError(f"{path}: replacement already present before composition")
    updated = text.replace(old, new, 1).encode("utf-8")
    path.write_bytes(updated)
    return digest(before), digest(updated)


def prepare(
    lean_root: Path,
    *,
    expected_blobs: dict[str, str] | None = None,
) -> dict[str, object]:
    root = lean_root.resolve()
    if not root.is_dir():
        raise ValueError("LEAN root does not exist")

    approved_blobs = EXPECTED_UPSTREAM_BLOBS if expected_blobs is None else expected_blobs
    if set(approved_blobs) != set(FILES):
        raise ValueError("approved LEAN blob map must exactly match composition files")

    # Validate the complete approved preimage set before mutating any file.
    # This prevents a late mismatch in one project from leaving an earlier
    # project partially composed.
    preimages: dict[str, tuple[Path, str]] = {}
    for relative, (old, new) in FILES.items():
        path = root / relative
        if not path.is_file():
            raise ValueError(f"missing approved LEAN file: {relative}")
        before_bytes, actual_blob = _canonical_preimage(root, relative, path)
        expected_blob = approved_blobs[relative]
        if actual_blob != expected_blob:
            raise ValueError(
                f"{relative}: upstream blob mismatch: expected {expected_blob}, got {actual_blob}"
            )
        try:
            text = before_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"{relative}: approved LEAN project is not UTF-8") from error
        if text.count(old) != 1:
            raise ValueError(
                f"{path}: expected exactly one approved composition anchor"
            )
        if new in text and old != new:
            raise ValueError(
                f"{path}: replacement already present before composition"
            )
        preimages[relative] = (path, actual_blob, before_bytes)

    records: list[dict[str, str]] = []
    for relative, (old, new) in FILES.items():
        path, actual_blob, before_bytes = preimages[relative]
        before_sha, after_sha = _replace_exact(path, before_bytes, old, new)
        records.append(
            {
                "path": relative,
                "upstream_blob_sha1": actual_blob,
                "before_sha256": before_sha,
                "after_sha256": after_sha,
            }
        )

    manifest = {
        "schema_version": "1.0.0",
        "composition_id": "autotrade-lean-security-composition-v1",
        "changes": records,
        "security_replacements": {
            "DotNetZip": {"from": "1.16.0", "to_package": "ProDotNetZip", "to": "1.20.0"},
            "System.Drawing.Common": {"minimum_transitive": "4.7.0", "pinned": "4.7.2"},
        },
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest["composition_sha256"] = digest(canonical.encode("utf-8"))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lean-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    manifest = prepare(args.lean_root)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(manifest["composition_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
