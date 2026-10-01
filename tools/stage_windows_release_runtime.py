"""Stage the complete source-controlled Windows release runtime in one transaction.

This module composes the already-reviewed foundation and neutral runtime
descriptor sets through the canonical staging primitives.  It adds one release
boundary property: the caller's exact expected source SHA is checked against an
identity-stable composition manifest while the canonical composition
publication lock is held, before any staging mutation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from tools.stage_windows_foundation import (
    ROOT,
    FoundationStagingError,
    _GIT_OBJECT_ID,
    _REQUIRED as _FOUNDATION_REQUIRED,
    _composition_publish_transaction,
    _parse_composition,
    _require_exact_regular_file,
    _retained_posix_directory,
    _retained_windows_directory,
    _same_file_identity,
    _stage_source_controlled_components_unserialized,
)
from tools.stage_windows_runtime import _RUNTIME_REQUIRED


_RELEASE_RUNTIME_REQUIRED = (*_FOUNDATION_REQUIRED, *_RUNTIME_REQUIRED)


def _require_expected_source_sha(
    composition_path: Path,
    *,
    expected_source_sha: str,
) -> None:
    if (
        type(expected_source_sha) is not str
        or _GIT_OBJECT_ID.fullmatch(expected_source_sha) is None
    ):
        raise FoundationStagingError(
            "expected_source_sha must be an exact lowercase Git object id"
        )
    info = _require_exact_regular_file(
        composition_path,
        label="composition manifest",
        single_link=True,
    )
    try:
        raw = composition_path.read_bytes()
    except OSError as error:
        raise FoundationStagingError("composition manifest cannot be read") from error
    if not _same_file_identity(composition_path, info):
        raise FoundationStagingError("composition manifest changed while reading")
    composition = _parse_composition(raw)
    if composition.get("source_sha") != expected_source_sha:
        raise FoundationStagingError(
            "composition source_sha does not match expected release source_sha"
        )


def stage_windows_release_runtime(
    *,
    staging: Path,
    composition_path: Path,
    expected_source_sha: str,
    source_root: Path = ROOT,
) -> tuple[dict[str, str], ...]:
    """Stage all 37 source-controlled runtime leaves under one authority window."""

    with _composition_publish_transaction(
        composition_path.parent
    ) as composition_authority:
        _require_expected_source_sha(
            composition_path,
            expected_source_sha=expected_source_sha,
        )
        if os.name == "nt":
            with _retained_windows_directory(
                source_root,
                label="source_root",
            ), _retained_windows_directory(
                staging,
                label="staging",
            ) as staging_authority:
                return _stage_source_controlled_components_unserialized(
                    staging=staging,
                    composition_path=composition_path,
                    source_root=source_root,
                    descriptors=_RELEASE_RUNTIME_REQUIRED,
                    staging_authority=staging_authority,
                )

        with _retained_posix_directory(
            source_root,
            label="source_root",
        ), _retained_posix_directory(
            staging,
            label="staging",
        ) as posix_staging_authority:
            return _stage_source_controlled_components_unserialized(
                staging=staging,
                composition_path=composition_path,
                source_root=source_root,
                descriptors=_RELEASE_RUNTIME_REQUIRED,
                posix_staging_authority=posix_staging_authority,
                posix_composition_authority=composition_authority,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--composition", required=True, type=Path)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = stage_windows_release_runtime(
            staging=args.staging,
            composition_path=args.composition,
            expected_source_sha=args.expected_source_sha,
            source_root=args.source_root,
        )
    except FoundationStagingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
