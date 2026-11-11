"""Import Groq keys from an owner-supplied local file into Windows credentials.

The source file is read locally and never copied into the repository. Output
contains only stable credential target aliases; key values and fingerprints are
deliberately omitted so this command is safe to capture in operational logs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "mvp"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from autotrade_mvp.model_credentials import ModelCredentialError, store_model_key


_GROQ_KEY = re.compile(rb"(?<![A-Za-z0-9_])gsk_[A-Za-z0-9]{32,96}(?![A-Za-z0-9_])")
_MAX_SOURCE_BYTES = 64 * 1024
_EXPECTED_ACCOUNT_COUNT = 4


class GroqCredentialImportError(RuntimeError):
    """Sanitized failure that never includes credential material."""


def extract_unique_groq_keys(source: Path) -> tuple[str, ...]:
    """Return exactly four distinct Groq keys without logging their values."""

    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise GroqCredentialImportError("Groq credential source must be an ordinary file")
    size = source.stat().st_size
    if size < 1 or size > _MAX_SOURCE_BYTES:
        raise GroqCredentialImportError("Groq credential source size is invalid")
    raw = bytearray(source.read_bytes())
    try:
        matches = {bytes(match) for match in _GROQ_KEY.findall(raw)}
        if len(matches) != _EXPECTED_ACCOUNT_COUNT:
            raise GroqCredentialImportError(
                "Groq credential source must contain exactly four distinct keys"
            )
        ordered = sorted(matches, key=lambda value: raw.find(value))
        return tuple(value.decode("ascii") for value in ordered)
    finally:
        for index in range(len(raw)):
            raw[index] = 0


def import_groq_credentials(
    source: Path,
    *,
    store: Callable[..., str] = store_model_key,
) -> dict[str, object]:
    keys = extract_unique_groq_keys(source)
    targets: list[str] = []
    for index, key in enumerate(keys, start=1):
        targets.append(
            store(provider="groq", profile=f"groq-account-{index}", key=key)
        )
    return {
        "schema": "autotrade-groq-credential-import-v1",
        "status": "STORED",
        "provider": "groq",
        "profile_count": len(targets),
        "targets": targets,
        "secret_material_emitted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Store four local Groq account keys in Windows Credential Manager"
    )
    parser.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        result = import_groq_credentials(Path(args.source))
    except (OSError, ValueError, ModelCredentialError, GroqCredentialImportError) as error:
        print(f"Groq credential import blocked: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
