"""Stage the complete source-controlled Windows release runtime.

Release staging composes the reviewed foundation and neutral runtime descriptor
sets through the canonical exact-Git source staging TCB.  The caller's expected
source SHA is mandatory: composition metadata alone must never be able to bless
caller-prepared runtime bytes as a release payload.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from tools.stage_windows_foundation import (
    ROOT,
    FoundationStagingError,
    _REQUIRED as _FOUNDATION_REQUIRED,
    _stage_source_controlled_components,
)
from tools.stage_windows_runtime import _RUNTIME_REQUIRED


_RELEASE_RUNTIME_REQUIRED = (*_FOUNDATION_REQUIRED, *_RUNTIME_REQUIRED)


def stage_windows_release_runtime(
    *,
    staging: Path,
    composition_path: Path,
    expected_source_sha: str,
    source_root: Path = ROOT,
) -> tuple[dict[str, str], ...]:
    """Stage the canonical 37-leaf release TCB from one exact Git commit."""

    return _stage_source_controlled_components(
        staging=staging,
        composition_path=composition_path,
        source_root=source_root,
        descriptors=_RELEASE_RUNTIME_REQUIRED,
        expected_source_sha=expected_source_sha,
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
