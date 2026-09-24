"""Reproducible zero-model qualification slice for AutoTrade.

This qualification deliberately provides no model implementation and performs no
network access.  It proves that RoutingMode.ZERO reserves no model cost while the
existing deterministic simulated financial slice remains replayable, reconciled,
and economically reportable.  It does not claim economic edge or live authority.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile

from mvp.autotrade_mvp.economics import build_economic_report
from mvp.autotrade_mvp.model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RouteStatus,
    RoutingMode,
    RoutingPolicy,
    route_model,
)
from mvp.autotrade_mvp.pipeline import run_vertical_slice, verify_replay


FIXED_NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
PRICES = ("100", "101", "102", "103")


def _require_source_sha(value: str) -> str:
    token = value.strip().lower()
    if len(token) != 40 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("source SHA must be an exact 40-character Git commit SHA")
    return token


def qualify(source_sha: str) -> dict[str, object]:
    source_sha = _require_source_sha(source_sha)

    request = ModelRequest(
        request_id="zero-model-qualification",
        allowed_model_ids=("unavailable-local", "unavailable-remote"),
        privacy_remote_allowed=False,
        budget_remaining=Decimal("0"),
        deadline_utc=FIXED_NOW + timedelta(minutes=5),
    )
    descriptors = (
        ModelDescriptor(
            model_id="unavailable-local",
            provider_id="unavailable",
            revision=None,
            remote=False,
            estimated_cost=Decimal("0"),
            latency_ms=0,
            quality_score=Decimal("0"),
        ),
        ModelDescriptor(
            model_id="unavailable-remote",
            provider_id="unavailable",
            revision=None,
            remote=True,
            estimated_cost=Decimal("1"),
            latency_ms=1,
            quality_score=Decimal("1"),
        ),
    )
    route = route_model(
        RoutingPolicy(
            mode=RoutingMode.ZERO,
            allowed_model_ids=tuple(item.model_id for item in descriptors),
            allow_remote=False,
            maximum_cost=Decimal("0"),
        ),
        request,
        descriptors,
        now_utc=FIXED_NOW,
    )
    if route.status is not RouteStatus.NO_MODEL:
        raise RuntimeError("ZERO routing unexpectedly admitted a model")
    if route.model_id is not None or route.provider_id is not None:
        raise RuntimeError("ZERO routing returned model/provider identity")
    if route.reserved_cost != Decimal("0"):
        raise RuntimeError("ZERO routing reserved model cost")

    with tempfile.TemporaryDirectory(prefix="autotrade-zero-model-") as directory:
        first = run_vertical_slice(PRICES, directory)
        second = run_vertical_slice(PRICES, directory)
        if not second.resumed:
            raise RuntimeError("deterministic slice did not prove restart/resume")
        if first.fill_id != second.fill_id or first.order_id != second.order_id:
            raise RuntimeError("restart changed deterministic economic identity")
        if first.cash != second.cash or first.position != second.position:
            raise RuntimeError("restart duplicated or changed economic state")
        if not first.reconciled or not second.reconciled or not verify_replay(directory):
            raise RuntimeError("zero-model deterministic slice failed reconciliation/replay")

        report = build_economic_report(directory)
        if report.economic_edge_claim != "UNPROVEN_SIMULATION_ONLY":
            raise RuntimeError("qualification must not manufacture an economic-edge claim")
        if report.evidence_count != 1:
            raise RuntimeError("replay duplicated immutable learning evidence")

        return {
            "qualification": "WP-62_ZERO_MODEL_FOUNDATION",
            "qualification_schema_version": "1.0.0",
            "source_sha": source_sha,
            "model_route": {
                "status": route.status.value,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
                "reserved_cost": str(route.reserved_cost),
                "reason": route.reason,
            },
            "deterministic_financial_slice": {
                "first_status": first.status,
                "restart_status": second.status,
                "resumed": second.resumed,
                "same_order_identity": first.order_id == second.order_id,
                "same_fill_identity": first.fill_id == second.fill_id,
                "reconciled": second.reconciled,
                "replay_verified": True,
            },
            "economics": report.as_jsonable(),
            "claims": {
                "live_trading_qualified": False,
                "economic_edge_proven": False,
                "all_wp62_workflows_qualified": False,
                "network_or_model_call_performed": False,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    evidence = qualify(args.source_sha)
    rendered = json.dumps(
        evidence,
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
