"""Bind one canonical historical campaign report to goal-objective receipts.

This adapter does not run a second simulator and does not select a champion.
It measures the same costed portfolio outcome against every configured
objective so future target-conditioned candidates remain directly comparable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from research.autotrade_research.agents.goal_directed import (
    SCHEMA,
    evaluate_objective,
    organism_manifest,
    workstream_from_document,
)
from research.autotrade_research.io.strict_json import strict_json_loads


MATRIX_SCHEMA = "autotrade-historical-objective-matrix-v1"


def _campaign_window_seconds(campaign: dict) -> int | None:
    """Canonical campaign aggregates cover inclusive calendar months, not days."""
    periods = campaign.get("periods")
    holdout = periods.get("holdout") if type(periods) is dict else None
    if type(holdout) is not list or len(holdout) != 2:
        return None
    try:
        start, last = [datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)
                       for value in holdout]
        if any(value != date.strftime("%Y-%m") for value, date in zip(holdout, (start, last))):
            return None
        end = last.replace(year=last.year + 1, month=1) if last.month == 12 else last.replace(month=last.month + 1)
    except (TypeError, ValueError):
        return None
    return int((end - start).total_seconds()) if last >= start else None


def _canonical(value: object) -> bytes:
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _read(path: Path, *, maximum_bytes: int = 10_000_000) -> tuple[object, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} is missing or redirected")
    payload = path.read_bytes()
    if len(payload) > maximum_bytes:
        raise ValueError(f"{path.name} is unbounded")
    return strict_json_loads(payload.decode("utf-8")), "sha256:" + sha256(payload).hexdigest()


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must use exact decimal text")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} is not finite")
    return result


def _atomic(path: Path, value: dict[str, object]) -> None:
    payload = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".objective-matrix-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def evaluate_campaign(*, campaign: object, campaign_sha256: str,
                      manifest: object, observed_at: datetime) -> dict[str, object]:
    if type(campaign) is not dict or campaign.get("status") != (
        "INTERNAL_TWO_ACCOUNT_HISTORICAL_CUT_NOT_PRODUCT_QUALIFICATION"
    ):
        raise ValueError("campaign report authority differs")
    if (type(campaign.get("symbol")) is not str
            or type(campaign.get("interval")) is not str
            or type(campaign.get("accounts")) is not dict
            or not campaign["accounts"]):
        raise ValueError("campaign report identity or accounts differ")
    if (type(manifest) is not dict or manifest.get("schema") != SCHEMA
            or type(manifest.get("workstreams")) is not list):
        raise ValueError("organism manifest schema differs")
    specs = tuple(workstream_from_document(row) for row in manifest["workstreams"])
    if organism_manifest(specs) != manifest:
        raise ValueError("organism manifest digest or canonical content differs")
    if type(observed_at) is not datetime or observed_at.tzinfo is None:
        raise TypeError("observed_at must be timezone-aware")
    complete_horizon = "qualification_cut" not in campaign
    campaign_window_seconds = _campaign_window_seconds(campaign) if complete_horizon else None
    accounts = {}
    for account_id, account in sorted(campaign["accounts"].items()):
        if type(account_id) is not str or type(account) is not dict:
            raise ValueError("campaign account schema differs")
        initial = _decimal(
            account.get("cash_baseline_equity_usd"), name="cash_baseline_equity_usd"
        )
        pnl = _decimal(account.get("agent_mark_pnl_usd"), name="agent_mark_pnl_usd")
        drawdown = _decimal(
            account.get("agent_max_mark_drawdown_fraction"),
            name="agent_max_mark_drawdown_fraction",
        )
        completed = account.get("completed_episodes")
        if type(completed) is not int or isinstance(completed, bool) or completed < 1:
            raise ValueError("campaign account has no completed episodes")
        receipts = [
            evaluate_objective(
                spec,
                initial_equity=initial,
                final_equity_after_costs=initial + pnl,
                max_drawdown_fraction=drawdown,
                horizon_complete=(complete_horizon and (
                    spec.profile.target_horizon_seconds is None
                    or campaign_window_seconds == spec.profile.target_horizon_seconds
                )),
            )
            for spec in specs
        ]
        for spec, receipt in zip(specs, receipts):
            receipt["target_horizon_seconds"] = spec.profile.target_horizon_seconds
            receipt["measured_window_seconds"] = campaign_window_seconds
            receipt["horizon_matches_target"] = (
                spec.profile.target_horizon_seconds is None
                or campaign_window_seconds == spec.profile.target_horizon_seconds
            )
        accounts[account_id] = {
            "initial_equity_usd": format(initial, "f"),
            "final_equity_after_costs_usd": format(initial + pnl, "f"),
            "agent_mark_pnl_usd": format(pnl, "f"),
            "max_drawdown_fraction": format(drawdown, "f"),
            "completed_episodes": completed,
            "objective_receipts": receipts,
        }
    body = {
        "schema": MATRIX_SCHEMA,
        "status": "MEASURED_EXISTING_CANDIDATE_NOT_TARGET_CONDITIONED_SELECTION",
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "campaign_report_sha256": campaign_sha256,
        "organism_manifest_sha256": manifest["manifest_sha256"],
        "symbol": campaign["symbol"],
        "interval": campaign["interval"],
        "horizon_complete": complete_horizon,
        "campaign_window_seconds": campaign_window_seconds,
        "accounts": accounts,
        "automatic_champion_promotion": False,
        "network_orders": False,
        "trading_authority": False,
    }
    return {**body, "matrix_sha256": "sha256:" + sha256(_canonical(body)).hexdigest()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate one canonical historical report against organism objectives"
    )
    parser.add_argument("--campaign-report", required=True)
    parser.add_argument("--organism-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        campaign, campaign_sha = _read(Path(args.campaign_report))
        manifest, _ = _read(Path(args.organism_root) / "organism.json")
        result = evaluate_campaign(
            campaign=campaign,
            campaign_sha256=campaign_sha,
            manifest=manifest,
            observed_at=datetime.now(timezone.utc),
        )
        _atomic(Path(args.output), result)
    except (OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": result["status"],
        "matrix_sha256": result["matrix_sha256"],
        "account_count": len(result["accounts"]),
        "horizon_complete": result["horizon_complete"],
        "network_orders": False,
        "trading_authority": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
