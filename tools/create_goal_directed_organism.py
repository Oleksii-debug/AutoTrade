"""Create one persistent multi-objective AutoTrade research-organism manifest."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from research.autotrade_research.agents.goal_directed import (
    PROFILE_IDS,
    create_workstream,
    organism_manifest,
)


def _atomic_write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False,
    ) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".goal-organism-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_manifest(*, root: Path, symbols: tuple[str, ...], created_at: datetime) -> dict[str, object]:
    if type(root) is not type(Path()):
        raise TypeError("root must be an exact Path")
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("root must be an ordinary directory")
    profiles = []
    for profile_id in PROFILE_IDS:
        session_id = profile_id.lower().replace("_", "-")
        spec = create_workstream(
            session_id=session_id,
            profile_id=profile_id,
            symbols=symbols,
            created_at=created_at,
            max_session_loss_fraction="0.05",
            max_position_fraction="0.25",
            # A return target is never authority to spend money.  A later,
            # explicit budget-ledger action may create a new manifest; the
            # unattended default remains local/deterministic for every mode.
            paid_model_budget_usd="0",
        )
        profiles.append(spec)
        memory = root / Path(spec.memory_namespace)
        memory.mkdir(parents=True, exist_ok=True)
        _atomic_write(memory / "workstream.json", spec.document())
    manifest = organism_manifest(profiles)
    _atomic_write(root / "organism.json", manifest)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create isolated strategy memories under one shared ZERO organism"
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--symbols", nargs="+", default=["BTC_USDT", "ETH_USDT"])
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(
            root=Path(args.root),
            symbols=tuple(args.symbols),
            created_at=datetime.now(timezone.utc),
        )
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "CREATED",
        "root": str(Path(args.root)),
        "manifest_sha256": manifest["manifest_sha256"],
        "workstream_count": len(manifest["workstreams"]),
        "network_orders": False,
        "trading_authority": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
