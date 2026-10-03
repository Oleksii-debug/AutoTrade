"""Compose the canonical staged Windows source closure before bundle creation.

This is the product-owned orchestration seam over the lower-level staging and
bundle authorities. It deliberately does not manufacture missing release
assets, SBOMs, locks, signatures, rights evidence, or release eligibility.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from tools.build_windows_bundle import DEFAULT_PROVENANCE, BundleError, build_bundle
from tools.stage_windows_foundation import (
    FoundationStagingError,
    ROOT,
    stage_windows_foundation,
)
from tools.stage_windows_host import stage_windows_host
from tools.stage_windows_runtime import stage_windows_runtime


_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


class WindowsProductBuildError(ValueError):
    """Raised before product staging when the build identity is invalid."""


def _source_sha(value: object) -> str:
    if type(value) is not str or _SOURCE_SHA.fullmatch(value) is None:
        raise WindowsProductBuildError(
            "source_sha must be an exact 40-character lowercase Git SHA"
        )
    return value


def stage_windows_product_source(
    *,
    staging: Path,
    composition_path: Path,
    source_sha: str,
    source_root: Path = ROOT,
) -> tuple[dict[str, str], ...]:
    """Stage one source-SHA-pinned foundation -> runtime -> host closure."""

    expected = _source_sha(source_sha)
    staged: list[dict[str, str]] = []
    for stage in (
        stage_windows_foundation,
        stage_windows_runtime,
        stage_windows_host,
    ):
        staged.extend(
            stage(
                staging=staging,
                composition_path=composition_path,
                source_root=source_root,
                expected_source_sha=expected,
            )
        )
    return tuple(staged)


def build_windows_product_bundle(
    *,
    staging: Path,
    output: Path,
    version: str,
    source_sha: str,
    mode: str,
    composition_path: Path,
    provenance_path: Path = DEFAULT_PROVENANCE,
    source_root: Path = ROOT,
) -> dict[str, object]:
    """Stage canonical source closure and only then invoke bundle authority."""

    expected = _source_sha(source_sha)
    stage_windows_product_source(
        staging=staging,
        composition_path=composition_path,
        source_sha=expected,
        source_root=source_root,
    )
    return build_bundle(
        staging=staging,
        output=output,
        version=version,
        source_sha=expected,
        mode=mode,
        provenance_path=provenance_path,
        composition_path=composition_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--mode", required=True, choices=("diagnostics", "release"))
    parser.add_argument("--composition", required=True, type=Path)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = build_windows_product_bundle(
            staging=args.staging,
            output=args.output,
            version=args.version,
            source_sha=args.source_sha,
            mode=args.mode,
            composition_path=args.composition,
            provenance_path=args.provenance,
            source_root=args.source_root,
        )
    except (BundleError, FoundationStagingError, WindowsProductBuildError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
