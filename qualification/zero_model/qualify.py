"""Reproducible zero-model qualification slice for AutoTrade.

This qualification deliberately provides no model implementation and performs no
network access.  It proves that RoutingMode.ZERO reserves no model cost while the
existing deterministic simulated financial slice remains replayable, reconciled,
and economically reportable.  It does not claim economic edge or live authority.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import tempfile
from unittest.mock import patch

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
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation


FIXED_NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
PRICES = ("100", "101", "102", "103")
AUTONOMOUS_PRICES = ("100", "101", "103", "102", "100", "98", "100", "103") * 15
AUTONOMOUS_NOW = "2026-10-03T00:00:00Z"
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_QUALIFIER_SOURCE_PATH = "qualification/zero_model/qualify.py"


class UnavailableModelInventory:
    """Sentinel iterable that fails if ZERO mode tries to inspect model inventory."""

    def __init__(self) -> None:
        self.touched = False

    def __iter__(self):
        self.touched = True
        raise RuntimeError("ZERO mode must not inspect unavailable model inventory")


@contextmanager
def _deny_network_access():
    """Fail before Python socket/DNS I/O can escape the zero-model qualifier."""

    attempts: list[str] = []

    def blocked(*args, **kwargs):
        del kwargs
        operation = "socket"
        if args:
            operation = getattr(args[0], "__name__", operation)
        attempts.append(str(operation))
        raise RuntimeError("zero-model qualification attempted network access")

    targets = (
        patch.object(socket.socket, "connect", blocked),
        patch.object(socket.socket, "connect_ex", blocked),
        patch.object(socket.socket, "sendto", blocked),
        patch.object(socket, "create_connection", blocked),
        patch.object(socket, "getaddrinfo", blocked),
        patch.object(socket, "gethostbyname", blocked),
        patch.object(socket, "gethostbyname_ex", blocked),
        patch.object(socket, "gethostbyaddr", blocked),
    )
    with (
        targets[0],
        targets[1],
        targets[2],
        targets[3],
        targets[4],
        targets[5],
        targets[6],
        targets[7],
    ):
        yield attempts


def _network_denied(function):
    """Wrap the whole qualifier in a fail-closed Python network fence."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with _deny_network_access() as network_attempts:
            evidence = function(*args, **kwargs)
        if network_attempts:
            raise RuntimeError(
                "zero-model qualification observed a blocked network attempt"
            )
        claims = evidence.get("claims")
        if type(claims) is not dict:
            raise RuntimeError("zero-model qualification evidence is missing claims")
        evidence["network_guard"] = {
            "python_socket_io_blocked": True,
            "network_attempt_count": 0,
        }
        claims["network_or_model_call_performed"] = False
        return evidence

    return wrapped


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


def _trusted_git_candidate_paths() -> tuple[Path, ...]:
    """Return fail-closed OS-managed Git locations without consulting PATH."""

    if os.name == "nt":
        return (
            Path(r"C:\Program Files\Git\cmd\git.exe"),
            Path(r"C:\Program Files\Git\bin\git.exe"),
        )
    return (Path("/usr/bin/git"), Path("/bin/git"))


def _trusted_git_executable() -> str:
    """Resolve Git independently of caller PATH and source-checkout content."""

    source_root = _SOURCE_ROOT.resolve(strict=True)
    for candidate in _trusted_git_candidate_paths():
        try:
            executable = candidate.resolve(strict=True)
        except OSError:
            continue
        if not executable.is_file():
            continue
        try:
            executable.relative_to(source_root)
        except ValueError:
            return os.fspath(executable)
        raise RuntimeError("trusted Git executable must not originate from source checkout")
    raise RuntimeError("trusted Git executable is unavailable at an OS-managed location")


def _trusted_git_environment() -> dict[str, str]:
    """Drop caller-selected repository/config/PATH authority from Git inspection."""

    environment = {
        key: value
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC")
        if (value := os.environ.get(key))
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    return environment


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    executable = _trusted_git_executable()
    try:
        return subprocess.run(
            [executable, *args],
            cwd=_SOURCE_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=_trusted_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("cannot inspect qualification Git checkout") from error


def _git_bytes(*args: str) -> subprocess.CompletedProcess[bytes]:
    executable = _trusted_git_executable()
    try:
        return subprocess.run(
            [executable, *args],
            cwd=_SOURCE_ROOT,
            check=True,
            capture_output=True,
            text=False,
            timeout=10,
            env=_trusted_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("cannot read exact qualification Git object") from error


def _observed_source_sha() -> str:
    """Read source identity from the exact checkout that owns this qualifier."""

    try:
        top_level = Path(_git("rev-parse", "--show-toplevel").stdout.strip()).resolve(
            strict=True
        )
        source_root = _SOURCE_ROOT.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("cannot verify qualification Git source root") from error
    if top_level != source_root:
        raise RuntimeError("qualification source root is not the exact Git top-level")
    return _require_source_sha(_git("rev-parse", "HEAD").stdout.strip())


def _require_clean_checkout() -> None:
    status = _git(
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout
    if status:
        raise RuntimeError(
            "qualification Git checkout has tracked or untracked source changes"
        )


def _require_exact_checkout(expected_source_sha: str) -> str:
    expected = _require_source_sha(expected_source_sha)
    observed = _observed_source_sha()
    if observed != expected:
        raise RuntimeError(
            "qualification source identity does not match actual Git checkout"
        )
    _require_clean_checkout()
    return observed


def _qualifier_sha256(source_sha: str) -> str:
    source_sha = _require_source_sha(source_sha)
    raw = _git_bytes(
        "cat-file",
        "blob",
        f"{source_sha}:{_QUALIFIER_SOURCE_PATH}",
    ).stdout
    return "sha256:" + sha256(raw).hexdigest()


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


@_network_denied
def qualify(source_sha: str) -> dict[str, object]:
    source_sha = _require_exact_checkout(source_sha)
    qualifier_sha256 = _qualifier_sha256(source_sha)

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

        autonomous_prices = list(AUTONOMOUS_PRICES)
        with (
            tempfile.TemporaryDirectory(prefix="autotrade-section16-continuous-") as continuous_directory,
            tempfile.TemporaryDirectory(prefix="autotrade-section16-restarted-") as restarted_directory,
        ):
            continuous = run_autonomous_simulation(
                autonomous_prices,
                continuous_directory,
                run_id="section16-zero-model-qualification",
                now=AUTONOMOUS_NOW,
            )
            paused = run_autonomous_simulation(
                autonomous_prices,
                restarted_directory,
                run_id="section16-zero-model-qualification",
                now=AUTONOMOUS_NOW,
                stop_after_episodes=41,
            )
            resumed = run_autonomous_simulation(
                autonomous_prices,
                restarted_directory,
                run_id="section16-zero-model-qualification",
                now=AUTONOMOUS_NOW,
            )
            replay = run_autonomous_simulation(
                autonomous_prices,
                restarted_directory,
                run_id="section16-zero-model-qualification",
                now=AUTONOMOUS_NOW,
            )

            if continuous["status"] != "COMPLETED" or resumed["status"] != "COMPLETED":
                raise RuntimeError("canonical autonomous ZERO loop did not complete")
            if paused["status"] != "PAUSED" or paused["completed_episodes"] != 41:
                raise RuntimeError("canonical autonomous ZERO loop did not produce the frozen pause cut")
            if continuous["completed_episodes"] != len(autonomous_prices):
                raise RuntimeError("canonical autonomous ZERO loop did not consume the complete population")
            if continuous["mode"] != "ZERO" or resumed["mode"] != "ZERO" or replay["mode"] != "ZERO":
                raise RuntimeError("canonical autonomous loop escaped ZERO mode")
            if (
                resumed["decisions"] != continuous["decisions"]
                or resumed["cash"] != continuous["cash"]
                or resumed["position"] != continuous["position"]
            ):
                raise RuntimeError("pause/resume changed canonical autonomous economics or decisions")
            if (
                paused["new_outbound_requests"] + resumed["new_outbound_requests"]
                != continuous["new_outbound_requests"]
            ):
                raise RuntimeError("pause/resume duplicated or lost canonical outbound requests")
            if replay["new_outbound_requests"] != 0:
                raise RuntimeError("completed autonomous replay emitted a duplicate outbound request")
            if replay["decisions"] != continuous["decisions"]:
                raise RuntimeError("completed autonomous replay changed decisions")
            if any(
                result["economic_edge_status"] != "INCONCLUSIVE"
                for result in (continuous, resumed, replay)
            ):
                raise RuntimeError("canonical autonomous qualification manufactured economic edge")

        with tempfile.TemporaryDirectory(prefix="autotrade-section16-partial-fill-") as partial_directory:
            partial_fill = run_autonomous_simulation(
                list(AUTONOMOUS_PRICES[:8]),
                partial_directory,
                run_id="section16-partial-fill-qualification",
                now=AUTONOMOUS_NOW,
                execution_profile="TWO_EQUAL_PARTIALS",
                target_quantity="2",
            )
            partial_fill_replay = run_autonomous_simulation(
                list(AUTONOMOUS_PRICES[:8]),
                partial_directory,
                run_id="section16-partial-fill-qualification",
                now=AUTONOMOUS_NOW,
                execution_profile="TWO_EQUAL_PARTIALS",
                target_quantity="2",
            )
            if partial_fill["status"] != "COMPLETED":
                raise RuntimeError("canonical partial-fill ZERO loop did not complete")
            if partial_fill["mode"] != "ZERO":
                raise RuntimeError("canonical partial-fill loop escaped ZERO mode")
            if partial_fill["new_outbound_requests"] <= 0:
                raise RuntimeError("canonical partial-fill loop did not exercise financial submission")
            if partial_fill_replay["new_outbound_requests"] != 0:
                raise RuntimeError("partial-fill replay emitted a duplicate outbound request")
            if partial_fill_replay["decisions"] != partial_fill["decisions"]:
                raise RuntimeError("partial-fill replay changed canonical decisions")
            if partial_fill["economic_edge_status"] != "INCONCLUSIVE":
                raise RuntimeError("partial-fill qualification manufactured economic edge")

        with tempfile.TemporaryDirectory(prefix="autotrade-section16-unknown-") as unknown_directory:
            unknown_first = run_autonomous_simulation(
                list(AUTONOMOUS_PRICES[:8]),
                unknown_directory,
                run_id="section16-unknown-qualification",
                now=AUTONOMOUS_NOW,
                fault_at_episode=3,
            )
            unknown_reentry = run_autonomous_simulation(
                list(AUTONOMOUS_PRICES[:8]),
                unknown_directory,
                run_id="section16-unknown-qualification",
                now=AUTONOMOUS_NOW,
                fault_at_episode=3,
            )
            if unknown_first["status"] != "UNKNOWN":
                raise RuntimeError("lost-response fault did not enter sticky UNKNOWN")
            if unknown_first["new_outbound_requests"] != 1:
                raise RuntimeError("lost-response qualification did not exercise exactly one send")
            if unknown_reentry["status"] != "UNKNOWN":
                raise RuntimeError("UNKNOWN state was not preserved across re-entry")
            if unknown_reentry["new_outbound_requests"] != 0:
                raise RuntimeError("UNKNOWN re-entry spontaneously resent financial intent")

        with tempfile.TemporaryDirectory(prefix="autotrade-section16-emergency-") as emergency_directory:
            emergency = run_autonomous_simulation(
                list(AUTONOMOUS_PRICES[:8]),
                emergency_directory,
                run_id="section16-emergency-qualification",
                now=AUTONOMOUS_NOW,
                emergency_at_episode=4,
            )
            emergency_replay = run_autonomous_simulation(
                list(AUTONOMOUS_PRICES[:8]),
                emergency_directory,
                run_id="section16-emergency-qualification",
                now=AUTONOMOUS_NOW,
                emergency_at_episode=4,
            )
            if emergency["status"] != "COMPLETED" or emergency["completed_episodes"] != 8:
                raise RuntimeError("emergency mode did not preserve deterministic loop completion")
            if emergency["new_outbound_requests"] != 1:
                raise RuntimeError("emergency mode did not stop subsequent new orders")
            if emergency_replay["new_outbound_requests"] != 0:
                raise RuntimeError("emergency replay emitted a duplicate outbound request")
            if emergency["economic_edge_status"] != "INCONCLUSIVE":
                raise RuntimeError("emergency qualification manufactured economic edge")

        source_sha = _require_exact_checkout(source_sha)

        return {
            "qualification": "WP-62_ZERO_MODEL_FOUNDATION",
            "execution_platform": {
                "system": platform.system(),
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
            },
            "qualification_schema_version": "1.0.0",
            "source_sha": source_sha,
            "observed_source_sha": source_sha,
            "source_checkout_clean": True,
            "qualifier_sha256": qualifier_sha256,
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
            "canonical_autonomous_zero_loop": {
                "continuous_status": continuous["status"],
                "paused_status": paused["status"],
                "resumed_status": resumed["status"],
                "replay_status": replay["status"],
                "mode": continuous["mode"],
                "episodes": continuous["completed_episodes"],
                "pause_cut": paused["completed_episodes"],
                "continuous_outbound_requests": continuous["new_outbound_requests"],
                "pause_resume_outbound_requests": (
                    paused["new_outbound_requests"] + resumed["new_outbound_requests"]
                ),
                "replay_outbound_requests": replay["new_outbound_requests"],
                "same_decisions_after_resume": resumed["decisions"] == continuous["decisions"],
                "same_economics_after_resume": (
                    resumed["cash"] == continuous["cash"]
                    and resumed["position"] == continuous["position"]
                ),
                "economic_edge_status": continuous["economic_edge_status"],
            },
            "canonical_unknown_no_resend": {
                "initial_status": unknown_first["status"],
                "reentry_status": unknown_reentry["status"],
                "initial_outbound_requests": unknown_first["new_outbound_requests"],
                "reentry_outbound_requests": unknown_reentry["new_outbound_requests"],
            },
            "canonical_emergency_zero_loop": {
                "status": emergency["status"],
                "episodes": emergency["completed_episodes"],
                "outbound_requests": emergency["new_outbound_requests"],
                "replay_outbound_requests": emergency_replay["new_outbound_requests"],
                "economic_edge_status": emergency["economic_edge_status"],
            },
            "canonical_partial_fill_zero_loop": {
                "status": partial_fill["status"],
                "mode": partial_fill["mode"],
                "new_outbound_requests": partial_fill["new_outbound_requests"],
                "replay_outbound_requests": partial_fill_replay["new_outbound_requests"],
                "same_decisions_after_replay": (
                    partial_fill_replay["decisions"] == partial_fill["decisions"]
                ),
                "economic_edge_status": partial_fill["economic_edge_status"],
                "execution_profile": "TWO_EQUAL_PARTIALS",
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
