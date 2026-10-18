"""Run and qualify one bounded, restart-tested historical ZERO cut.

This command composes the existing campaign, canonical journals/checkpoints,
independent diagnostic exporters and the read-only E2E qualifier.  A bounded
cut proves wiring and recovery only; it never claims full-month performance.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any

from mvp.autotrade_mvp.diagnostic_export import (
    agent_report_zip,
    technical_diagnostic_zip,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_diagnostic_sampler import RuntimeDiagnosticSampler
from tools.qualify_historical_e2e import qualify_workspace
from tools.run_historical_campaign import campaign
from tools.train_historical_agent import save_model


SOURCE_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
IDENTITY_SCHEMA = "autotrade-historical-e2e-identity-v1"
RESTART_SCHEMA = "autotrade-historical-e2e-restart-v1"


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _journal_sequence(path: Path) -> int:
    store = JournalStore(path)
    return store.current_journal_sequence()


def _account_paths(workspace: Path, *, symbol: str, holdout_from: str,
                   holdout_through: str) -> dict[str, Path]:
    base = workspace / "accounts" / symbol / f"{holdout_from}-{holdout_through}"
    return {budget: base / f"usd-{budget}" for budget in ("200", "1000")}


def _runtime_cut(account: Path) -> dict[str, Any]:
    checkpoint = account / "autonomous-runtime-checkpoint.json"
    journal = account / "journal.sqlite3"
    if not checkpoint.is_file() or not journal.is_file():
        raise ValueError("campaign did not publish a durable runtime cut")
    try:
        document = json.loads(checkpoint.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime checkpoint is malformed") from exc
    build = document.get("build_sha") if type(document) is dict else None
    if type(build) is not str or re.fullmatch(r"[0-9a-f]{64}", build) is None:
        raise ValueError("runtime checkpoint has no exact build identity")
    sequence = _journal_sequence(journal)
    if sequence < 1:
        raise ValueError("runtime journal is empty")
    return {
        "build_sha256": build,
        "checkpoint_sha256": _file_digest(checkpoint),
        "journal_sequence": sequence,
    }


def _publish_bytes(directory: Path, filename: str, body: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    if destination.exists() or destination.is_symlink():
        if destination.is_file() and destination.read_bytes() == body:
            return destination
        raise ValueError("existing E2E evidence differs")
    descriptor, temporary = tempfile.mkstemp(prefix=".historical-e2e-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)
    return destination


def run_qualification_cut(
    *,
    data_root: Path,
    workspace: Path,
    source_sha: str,
    symbol: str,
    train_from: str,
    train_through: str,
    validate_from: str,
    validate_through: str,
    holdout_from: str,
    holdout_through: str,
    episode_limit: int = 12,
) -> dict[str, Any]:
    """Produce a real bounded campaign cut, restart it, and qualify its evidence."""
    if type(source_sha) is not str or SOURCE_SHA.fullmatch(source_sha) is None:
        raise ValueError("source SHA must be exact lowercase Git hex")
    if type(workspace) is not type(Path()) or type(data_root) is not type(Path()):
        raise TypeError("data root and workspace must be exact Path values")
    if workspace.exists() and (workspace.is_symlink() or not workspace.is_dir()):
        raise ValueError("workspace must be an ordinary directory")
    workspace.mkdir(parents=True, exist_ok=True)
    technical_root = workspace / "technical-diagnostics"
    technical_root.mkdir(parents=True, exist_ok=True)
    parameters = dict(
        data_root=data_root,
        workspace=workspace,
        symbol=symbol,
        train_from=train_from,
        train_through=train_through,
        validate_from=validate_from,
        validate_through=validate_through,
        holdout_from=holdout_from,
        holdout_through=holdout_through,
        episode_limit=episode_limit,
        qualification_cut=True,
    )

    # First process generation creates the cut. The second starts from disk and
    # must be a no-op replay: identical checkpoint and journal sequence.
    sampler = RuntimeDiagnosticSampler(technical_root, interval_seconds=60.0)
    sampler.start()
    try:
        first_report = campaign(**parameters)
        sampler.sample_once()
    finally:
        sampler.stop()
    accounts = _account_paths(
        workspace,
        symbol=symbol,
        holdout_from=holdout_from,
        holdout_through=holdout_through,
    )
    before = {budget: _runtime_cut(path) for budget, path in accounts.items()}
    sampler = RuntimeDiagnosticSampler(technical_root, interval_seconds=60.0)
    sampler.start()
    try:
        resumed_report = campaign(**parameters)
        sampler.sample_once()
        technical = technical_diagnostic_zip(sampler, source_revision=source_sha)
    finally:
        sampler.stop()
    if resumed_report != first_report:
        raise ValueError("restart changed the frozen bounded campaign report")
    after = {budget: _runtime_cut(path) for budget, path in accounts.items()}
    builds = {row["build_sha256"] for row in after.values()}
    if len(builds) != 1:
        raise ValueError("account checkpoints do not share one exact build")
    build_sha256 = next(iter(builds))

    restart_rows = {}
    for budget, account in accounts.items():
        relative = account.relative_to(workspace).as_posix()
        prior, current = before[budget], after[budget]
        unchanged = (
            prior["checkpoint_sha256"] == current["checkpoint_sha256"]
            and prior["journal_sequence"] == current["journal_sequence"]
        )
        if not unchanged:
            raise ValueError("no-op restart duplicated or changed financial evidence")
        restart_rows[relative] = {
            "checkpoint_sha256_before": prior["checkpoint_sha256"],
            "checkpoint_sha256_after": current["checkpoint_sha256"],
            "journal_sequence_before": prior["journal_sequence"],
            "journal_sequence_after": current["journal_sequence"],
            "recovered_without_duplicate_effects": True,
        }
    qualification = workspace / "qualification"
    save_model(qualification / "restart-evidence.json", {
        "schema": RESTART_SCHEMA,
        "status": "PASS",
        "source_sha": source_sha,
        "build_sha256": build_sha256,
        "accounts": restart_rows,
    })

    immutable = {
        path.relative_to(workspace).as_posix(): _file_digest(path)
        for directory in ("models", "validation", "reports")
        for path in sorted((workspace / directory).glob("*.json"))
    }
    save_model(qualification / "identity.json", {
        "schema": IDENTITY_SCHEMA,
        "source_sha": source_sha,
        "build_sha256": build_sha256,
        "mode": "ZERO",
        "provider_credentials": "NONE",
        "real_order_route": "UNAVAILABLE",
        "network_orders": False,
        "immutable_artifacts": immutable,
    })

    diagnostics = workspace / "diagnostics"
    for budget, account in accounts.items():
        exported = agent_report_zip(
            JournalStore(account / "journal.sqlite3"),
            account,
            source_revision=source_sha,
        )
        _publish_bytes(
            diagnostics,
            f"autotrade-agent-report-usd-{budget}-{exported.sha256[:12]}.zip",
            exported.data,
        )
    _publish_bytes(
        diagnostics,
        f"autotrade-technical-diagnostics-{technical.sha256[:12]}.zip",
        technical.data,
    )

    result = qualify_workspace(
        workspace,
        source_sha=source_sha,
        build_sha256=build_sha256,
    )
    if result["status"] != "PASS":
        raise ValueError("bounded historical E2E cut did not qualify: " + result["status"])
    save_model(qualification / "historical-e2e-report.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded causal ZERO campaign and exact restart qualification"
    )
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    for name in (
        "train-from", "train-through", "validate-from", "validate-through",
        "holdout-from", "holdout-through",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--episode-limit", type=int, default=12)
    args = parser.parse_args(argv)
    try:
        report = run_qualification_cut(
            data_root=Path(args.data_root),
            workspace=Path(args.workspace),
            source_sha=args.source_sha,
            symbol=args.symbol,
            train_from=args.train_from,
            train_through=args.train_through,
            validate_from=args.validate_from,
            validate_through=args.validate_through,
            holdout_from=args.holdout_from,
            holdout_through=args.holdout_through,
            episode_limit=args.episode_limit,
        )
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, ArithmeticError, KeyError, sqlite3.Error) as exc:
        print(f"AutoTrade bounded historical E2E blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
