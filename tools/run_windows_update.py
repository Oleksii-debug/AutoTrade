"""CLI entry point for the audited AutoTrade Windows updater bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from mvp.autotrade_mvp.windows_update_bridge import (
    WindowsUpdateBridgeError,
    WindowsUpdateBridgeRequest,
    run_windows_update,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install one qualified AutoTrade update")
    parser.add_argument("--descriptor-url", required=True)
    parser.add_argument("--descriptor-sha256", required=True)
    parser.add_argument("--current-release-manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--controller-executable", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_windows_update(
            WindowsUpdateBridgeRequest(
                descriptor_url=args.descriptor_url,
                descriptor_sha256=args.descriptor_sha256,
                current_release_manifest=args.current_release_manifest.absolute(),
                evidence_root=args.evidence_root.absolute(),
                state_dir=args.state_dir.absolute(),
                artifact_root=args.artifact_root.absolute(),
                data_root=args.data_root.absolute(),
                install_root=args.install_root.absolute(),
                backup_root=args.backup_root.absolute(),
                work_root=args.work_root.absolute(),
                controller_executable=args.controller_executable.absolute(),
            )
        )
    except WindowsUpdateBridgeError as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
