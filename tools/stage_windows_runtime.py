"""Stage the reviewed neutral AutoTrade runtime through the canonical source TCB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from tools.stage_windows_foundation import (
    ROOT,
    FoundationStagingError,
    _SourceControlledComponent,
    _stage_source_controlled_components,
)


_RUNTIME_REQUIRED = (
    _SourceControlledComponent(
        "autotrade-runtime-01-init--",
        "runtime-core",
        "autotrade_runtime/__init__.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-02-resource-lock",
        "runtime-core",
        "autotrade_runtime/resource_lock.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-03-strict-json",
        "runtime-core",
        "autotrade_runtime/strict_json.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-04-init--",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/__init__.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-05-crash-atomic-manifest",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_crash_atomic_manifest.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-06-crash-atomic-publication",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_crash_atomic_publication.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-07-generation-bound-read",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_generation_bound_read.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-08-manifest-descriptor-io",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_manifest_descriptor_io.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-09-namespace-guard",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_namespace_guard.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-10-posix-retained-object-move-fix",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_posix_retained_object_move_fix.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-11-publication-contract-compat",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_publication_contract_compat.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-12-publication-transaction-fix",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_publication_transaction_fix.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-13-race-regressions",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_race_regressions.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-14-retained-coordination",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_coordination.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-15-retained-namespace",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_namespace.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-16-retained-namespace-hardening",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_namespace_hardening.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-17-retained-path-compat",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_path_compat.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-18-retained-publication",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_publication.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-19-retained-publication-hardening",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_publication_hardening.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-20-retained-recovery-final",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_recovery_final.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-21-retained-recovery-hardening",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_retained_recovery_hardening.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-22-root-authority",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_root_authority.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-23-root-authority-failure-fix",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_root_authority_failure_fix.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-24-stable-posix-capabilities",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_stable_posix_capabilities.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-25-windows-descriptor-bridge",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_windows_descriptor_bridge.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-26-windows-retained-publication",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_windows_retained_publication.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-27-windows-retained-publication-hardening",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_windows_retained_publication_hardening.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-28-windows-retained-rename-fix",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/_windows_retained_rename_fix.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-29-durable-publish",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/durable_publish.py",
    ),
    _SourceControlledComponent(
        "autotrade-runtime-30-store",
        "runtime-artifacts",
        "autotrade_runtime/artifacts/store.py",
    ),
)


def stage_windows_runtime(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path = ROOT,
) -> tuple[dict[str, str], ...]:
    """Stage the module-owned neutral runtime/artifact descriptor set."""

    return _stage_source_controlled_components(
        staging=staging,
        composition_path=composition_path,
        source_root=source_root,
        descriptors=_RUNTIME_REQUIRED,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--composition", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = stage_windows_runtime(
            staging=args.staging,
            composition_path=args.composition,
            source_root=args.source_root,
        )
    except FoundationStagingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
