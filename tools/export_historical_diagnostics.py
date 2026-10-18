"""Downloadable diagnostic report for an explicit historical workspace."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

from mvp.autotrade_mvp.diagnostic_export import historical_diagnostic_export


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export simulated learning, decisions and results as a shareable JSON report")
    parser.add_argument("--workspace", required=True, help="Research workspace passed to the campaign")
    parser.add_argument("--output", required=True, help="New JSON report file to download and share")
    args = parser.parse_args(argv)
    try:
        report = historical_diagnostic_export(Path(args.workspace))
        destination = Path(args.output).absolute()
        if destination.suffix.lower() != ".json" or not destination.parent.is_dir():
            raise ValueError("output must be a JSON file in an existing directory")
        if destination.exists() or destination.is_symlink():
            raise ValueError("output already exists; choose a new filename")
        from tools.historical_public_klines import _reject_symlink_ancestors
        _reject_symlink_ancestors(destination.parent)
        descriptor, temporary = tempfile.mkstemp(prefix=".autotrade-diagnostic-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(report.data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)
        finally:
            os.unlink(temporary)
        print(f"Diagnostic report: {destination}\nBytes: {len(report.data)}\nSHA-256: {report.sha256}")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"AutoTrade diagnostic export blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
