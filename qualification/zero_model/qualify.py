"""Reproducible zero-model qualification slice for AutoTrade.

This qualification deliberately provides no model implementation and performs no
network access.  It proves that RoutingMode.ZERO reserves no model cost while the
existing deterministic simulated financial slice remains replayable, reconciled,
and economically reportable.  It does not claim economic edge or live authority.
"""

from __future__ import annotations

import argparse
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
from mvp.autotrade_mvp.pipeline import run_multi_episode, run_vertical_slice, verify_replay


FIXED_NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
PRICES = ("100", "101", "102", "103")


class UnavailableModelInventory:
    """Sentinel iterable that fails if ZERO mode tries to inspect model inventory."""

    def __init__(self) -> None:
        self.touched = False

    def __iter__(self):
        self.touched = True
        raise RuntimeError("ZERO mode must not inspect unavailable model inventory")


def _require_source_sha(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("source SHA must be text")
    if (
        len(value) not in {40, 64}
        or value != value.strip()
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(
            "source SHA must be a canonical lowercase 40- or 64-character Git object id"
        )
    return value


def _outage_routes() -> dict[str, object]:
    """Model routing failures that must not become hidden paid fallbacks."""

    remote_request = ModelRequest(
        request_id="remote-outage",
        allowed_model_ids=("remote-only",),
        privacy_remote_allowed=True,
        budget_remaining=Decimal("5"),
        deadline_utc=FIXED_NOW + timedelta(minutes=5),
    )
    remote = route_model(
        RoutingPolicy(
            mode=RoutingMode.DYNAMIC,
            allowed_model_ids=remote_request.allowed_model_ids,
            allow_remote=True,
            maximum_cost=Decimal("5"),
            maximum_latency_ms=5_000,
        ),
        remote_request,
        (),
        now_utc=FIXED_NOW,
    )
    if remote.status is not RouteStatus.NO_MODEL or remote.reserved_cost != Decimal("0"):
        raise RuntimeError("remote outage admitted or reserved model spend")

    local_request = ModelRequest(
        request_id="local-resource-exhaustion",
        allowed_model_ids=("local-overloaded",),
        privacy_remote_allowed=False,
        budget_remaining=Decimal("0"),
        deadline_utc=FIXED_NOW + timedelta(minutes=5),
    )
    local = route_model(
        RoutingPolicy(
            mode=RoutingMode.LOCAL_ONLY,
            allowed_model_ids=local_request.allowed_model_ids,
            allow_remote=False,
            maximum_cost=Decimal("0"),
            maximum_latency_ms=1_000,
        ),
        local_request,
        (
            ModelDescriptor(
                model_id="local-overloaded",
                provider_id="local-runtime",
                revision="load-test-v1",
                remote=False,
                estimated_cost=Decimal("0"),
                latency_ms=60_000,
                quality_score=Decimal("0.90"),
            ),
        ),
        now_utc=FIXED_NOW,
    )
    if local.status is not RouteStatus.NO_MODEL or local.reserved_cost != Decimal("0"):
        raise RuntimeError("local resource exhaustion admitted or reserved model spend")

    return {
        "remote_outage": remote,
        "local_resource_exhaustion": local,
    }


def qualify(source_sha: str) -> dict[str, object]:
    source_sha = _require_source_sha(source_sha)

    request = ModelRequest(
        request_id="zero-model-qualification",
        allowed_model_ids=("unavailable-local", "unavailable-remote"),
        privacy_remote_allowed=False,
        budget_remaining=Decimal("0"),
        deadline_utc=FIXED_NOW + timedelta(minutes=5),
    )
    inventory = UnavailableModelInventory()
    route = route_model(
        RoutingPolicy(
            mode=RoutingMode.ZERO,
            allowed_model_ids=request.allowed_model_ids,
            allow_remote=False,
            maximum_cost=Decimal("0"),
        ),
        request,
        inventory,
        now_utc=FIXED_NOW,
    )
    if route.status is not RouteStatus.NO_MODEL:
        raise RuntimeError("ZERO routing unexpectedly admitted a model")
    if route.model_id is not None or route.provider_id is not None:
        raise RuntimeError("ZERO routing returned model/provider identity")
    if route.reserved_cost != Decimal("0"):
        raise RuntimeError("ZERO routing reserved model cost")
    if inventory.touched:
        raise RuntimeError("ZERO routing inspected unavailable model inventory")

    outage_routes = _outage_routes()
    if any(decision.model_id is not None or decision.provider_id is not None for decision in outage_routes.values()):
        raise RuntimeError("outage routing returned a hidden fallback identity")
    model_cost_total = route.reserved_cost + sum(
        (decision.reserved_cost for decision in outage_routes.values()),
        Decimal("0"),
    )
    if model_cost_total != Decimal("0"):
        raise RuntimeError("zero-model outage qualification reserved model spend")

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

        with tempfile.TemporaryDirectory(prefix="autotrade-small-capital-") as small_directory:
            small_first = run_vertical_slice(
                PRICES,
                small_directory,
                initial_cash=Decimal("1"),
            )
            small_second = run_vertical_slice(
                PRICES,
                small_directory,
                initial_cash=Decimal("1"),
            )
            if small_first.status != "risk_rejected" or small_second.status != "risk_rejected":
                raise RuntimeError("small-capital slice must fail closed at the risk gate")
            if small_first.order_id is not None or small_first.fill_id is not None:
                raise RuntimeError("small-capital rejection created financial execution identity")
            if not small_second.resumed or not verify_replay(small_directory):
                raise RuntimeError("small-capital rejection did not remain replayable")
            small_report = build_economic_report(small_directory)
            if small_report.trade_count != 0 or small_report.net_pnl != Decimal("0"):
                raise RuntimeError("small-capital rejection changed economic state")

        with tempfile.TemporaryDirectory(prefix="autotrade-zero-model-campaign-") as campaign_directory:
            episodes = (
                ("100", "101", "102", "103"),
                ("103", "102", "101", "100"),
            )
            campaign_first = run_multi_episode(episodes, campaign_directory)
            first_report = build_economic_report(campaign_directory)
            campaign_second = run_multi_episode(episodes, campaign_directory)
            second_report = build_economic_report(campaign_directory)

            if not all(result.reconciled for result in (*campaign_first, *campaign_second)):
                raise RuntimeError("zero-model campaign failed reconciliation")
            if not all(result.resumed for result in campaign_second):
                raise RuntimeError("zero-model campaign did not prove restart/resume")
            if [result.order_id for result in campaign_first] != [result.order_id for result in campaign_second]:
                raise RuntimeError("zero-model campaign changed deterministic order identity")
            if [result.fill_id for result in campaign_first] != [result.fill_id for result in campaign_second]:
                raise RuntimeError("zero-model campaign changed deterministic fill identity")
            if first_report != second_report:
                raise RuntimeError("zero-model campaign changed economics after replay")
            if first_report.trade_count != 2 or first_report.evidence_count != 2:
                raise RuntimeError("zero-model campaign did not preserve two independent episodes")
            if first_report.ending_position != Decimal("0"):
                raise RuntimeError("zero-model campaign did not return to flat position")
            if first_report.economic_edge_claim != "UNPROVEN_SIMULATION_ONLY":
                raise RuntimeError("zero-model campaign manufactured an economic-edge claim")

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
                "model_inventory_touched": inventory.touched,
            },
            "outage_routes": {
                name: {
                    "status": decision.status.value,
                    "model_id": decision.model_id,
                    "provider_id": decision.provider_id,
                    "reserved_cost": str(decision.reserved_cost),
                    "reason": decision.reason,
                }
                for name, decision in outage_routes.items()
            },
            "model_cost_total": str(model_cost_total),
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
            "multi_episode_economics": {
                "episode_statuses": [result.status for result in campaign_first],
                "replay_statuses": [result.status for result in campaign_second],
                "restart_resumed": all(result.resumed for result in campaign_second),
                "same_order_identities": [result.order_id for result in campaign_first] == [result.order_id for result in campaign_second],
                "same_fill_identities": [result.fill_id for result in campaign_first] == [result.fill_id for result in campaign_second],
                "reconciled": all(result.reconciled for result in campaign_second),
                "economics": second_report.as_jsonable(),
            },
            "small_capital": {
                "status": small_second.status,
                "resumed": small_second.resumed,
                "order_id": small_second.order_id,
                "fill_id": small_second.fill_id,
                "reconciled": small_second.reconciled,
                "replay_verified": True,
                "economics": small_report.as_jsonable(),
            },
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
