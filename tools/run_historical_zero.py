"""Research-only historical candle adapter for the existing AutoTrade ZERO engine.

No broker credentials, no network orders. This is an integration experiment.
The optional next-bar cut separates strategy observations from execution and
waits for the immediately following complete bar. Its adverse OHLCV scenario
is not observed order-book liquidity or publisher point-in-time proof.
It is NOT qualified historical profitability evidence.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from hashlib import sha256
import json
from pathlib import Path
import re

from autotrade_runtime.artifacts import ArtifactIntegrityError, ArtifactStore

from tools.historical_public_klines import (
    BASE, DAILY_BASE, DATASET_LICENSE, DATASET_TERMS_URL,
    HistoricalArchiveError, INTERVAL_SECONDS, SYMBOL, _month_range,
    _reject_symlink_ancestors, _utc_day, _require_past_month,
)
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation
from research.autotrade_research.io.strict_json import strict_json_loads


MAX_STRATEGY_PLAN_BYTES = 5_000_000


def _read_strategy_plan(
    path: Path,
    *,
    dataset_identity_sha256: str,
    symbol: str,
    interval: str,
    observation_count: int,
    signal_prices: list[str],
    available_at: list[datetime],
) -> dict[str, object]:
    """Read and reconstruct one causal plan without trusting its own digest."""
    _reject_symlink_ancestors(path)
    if (path.is_symlink() or not path.is_file()
            or path.stat().st_size > MAX_STRATEGY_PLAN_BYTES):
        raise HistoricalArchiveError(
            "registered deterministic strategy plan is missing, redirected or unbounded"
        )
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"))
        from research.autotrade_research.strategies.target_matrix import (
            validate_strategy_plan,
        )
        return validate_strategy_plan(
            raw,
            dataset_identity_sha256="sha256:" + dataset_identity_sha256,
            symbol=symbol,
            interval=interval,
            observation_count=observation_count,
            signal_prices=signal_prices,
            available_at=available_at,
            interval_seconds=INTERVAL_SECONDS[interval],
        )
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise HistoricalArchiveError(
            "registered deterministic strategy plan is invalid"
        ) from exc


def _verify_artifact_evidence(
    *,
    store: ArtifactStore,
    manifest: dict,
    normalized_csv: bytes,
) -> None:
    if type(store) is not ArtifactStore:
        raise HistoricalArchiveError("artifact_store must be the canonical ArtifactStore")
    evidence = manifest.get("artifact_evidence")
    roles = {"source_zip", "publisher_checksum", "normalized_ohlcv_csv"}
    if type(evidence) is not dict or set(evidence) != roles:
        raise HistoricalArchiveError("historical manifest lacks canonical artifact evidence")
    expected_digests = {
        "source_zip": "sha256:" + manifest["source_zip_sha256"],
        "publisher_checksum": "sha256:" + manifest["source_checksum_sha256"],
        "normalized_ohlcv_csv": "sha256:" + manifest["normalized_csv_sha256"],
    }
    expected_media = {
        "source_zip": "application/zip",
        "publisher_checksum": "text/plain; charset=us-ascii",
        "normalized_ohlcv_csv": "text/csv; charset=utf-8",
    }
    expected_sources = {
        "source_zip": [manifest["source_url"], manifest["source_checksum_url"], DATASET_TERMS_URL],
        "publisher_checksum": [manifest["source_checksum_url"], manifest["source_url"], DATASET_TERMS_URL],
        "normalized_ohlcv_csv": [manifest["source_url"], manifest["source_checksum_url"], DATASET_TERMS_URL],
    }
    try:
        for role in sorted(roles):
            reference = evidence[role]
            if type(reference) is not dict or set(reference) != {
                "artifact_id", "sha256", "bytes", "media_type", "rights_id",
                "export_permitted",
            }:
                raise HistoricalArchiveError("historical artifact reference is malformed")
            authenticated, data = store.read_authenticated_snapshot(reference["artifact_id"])
            if any((
                reference["sha256"] != expected_digests[role],
                reference["sha256"] != authenticated.get("sha256"),
                reference["bytes"] != authenticated.get("bytes"),
                reference["media_type"] != expected_media[role],
                reference["media_type"] != authenticated.get("media_type"),
                reference["rights_id"] != "binance-vision-noncommercial-research-v1",
                reference["export_permitted"] is not False,
                authenticated.get("rights") != {
                    "storage": True,
                    "export": False,
                    "rights_id": "binance-vision-noncommercial-research-v1",
                },
                authenticated.get("source_refs") != expected_sources[role],
            )):
                raise HistoricalArchiveError("historical artifact evidence conflicts with provenance")
            metadata = authenticated.get("metadata")
            if type(metadata) is not dict or any(metadata.get(key) != expected for key, expected in (
                ("exchange", "BINANCE_SPOT"),
                ("symbol", manifest["symbol"]),
                ("interval", manifest["interval"]),
                ("period", manifest["day"] or manifest["month"]),
                ("license_terms_url", DATASET_TERMS_URL),
                ("rights_scope", "PERSONAL_NON_COMMERCIAL_RESEARCH_SIMULATION_ONLY"),
                ("evidence_kind", role.upper()),
            )):
                raise HistoricalArchiveError("historical artifact metadata conflicts with provenance")
            if role == "normalized_ohlcv_csv" and data != normalized_csv:
                raise HistoricalArchiveError("artifact-store normalized CSV differs from replay bytes")
    except (ArtifactIntegrityError, FileNotFoundError, KeyError, OSError,
            RuntimeError, TypeError, ValueError) as exc:
        if isinstance(exc, HistoricalArchiveError):
            raise
        raise HistoricalArchiveError("historical artifact evidence verification failed") from exc


def _checked_month(
    root: Path,
    symbol: str,
    interval: str,
    month: str,
    *,
    artifact_store: ArtifactStore | None = None,
    day: str | None = None,
    include_ohlcv: bool = False,
):
    """Validate one frozen UTC period; daily and monthly paths share authority."""
    if type(symbol) is not str or SYMBOL.fullmatch(symbol) is None or interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("invalid instrument selection")
    if day is None:
        _require_past_month(month)
    elif _utc_day(day).strftime("%Y-%m") != month:
        raise HistoricalArchiveError("daily period does not match month")
    period = day or month
    base = root / "spot" / symbol / interval
    if day is not None:
        base = base / "daily"
    # Verify the complete lexical ancestor chain, including ancestors ABOVE
    # data-root; a symlink above root must never redirect trusted replay.
    _reject_symlink_ancestors(base)
    manifest_file = base / (period + ".manifest.json")
    rows_file = base / (period + ".ohlcv.csv")
    for f in (manifest_file, rows_file):
        if f.is_symlink() or not f.is_file():
            raise HistoricalArchiveError(f"missing/nonregular verified market archive for {month}")
    mraw = manifest_file.read_bytes()
    raw = rows_file.read_bytes()
    if len(mraw) > 20_000 or len(raw) > 80 * 1024 * 1024:
        raise HistoricalArchiveError("unbounded historical input")
    try:
        manifest = json.loads(mraw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalArchiveError("invalid source provenance manifest") from exc
    if type(manifest) is not dict or any(manifest.get(key) != expected for key, expected in (
        ("schema", "autotrade-spot-monthly-public-klines-v1"),
        ("symbol", symbol), ("interval", interval), ("month", month),
        ("market", "BINANCE_PUBLIC_ARCHIVE_SPOT_NOT_TRADING_PROVIDER"),
    )):
        raise HistoricalArchiveError("source identity conflicts with requested archive")
    # A normalized CSV digest alone does not authenticate a locally mutable
    # provenance manifest. Reject origin/right-scope relabeling before replay.
    source_base = DAILY_BASE if day is not None else BASE
    canonical_zip_url = f"{source_base}/{symbol}/{interval}/{symbol}-{interval}-{period}.zip"
    if any(manifest.get(key) != expected for key, expected in (
        ("day", day),
        ("period_granularity", "DAILY" if day is not None else "MONTHLY"),
        ("exchange", "BINANCE_SPOT"),
        ("source_url", canonical_zip_url),
        ("source_checksum_url", canonical_zip_url + ".CHECKSUM"),
        ("license", DATASET_LICENSE),
        ("license_terms_url", DATASET_TERMS_URL),
        ("rights_scope", "PERSONAL_NON_COMMERCIAL_RESEARCH_SIMULATION_ONLY"),
    )):
        raise HistoricalArchiveError("source provenance metadata conflicts with selected public archive")
    for field in ("source_zip_sha256", "source_csv_sha256", "source_checksum_sha256"):
        value = manifest.get(field)
        if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise HistoricalArchiveError(f"source provenance digest invalid: {field}")
    if not manifest.get("usable_as_complete_causal_interval") or manifest.get("missing_candle_intervals") != 0:
        raise HistoricalArchiveError("historical source has missing intervals; fail closed")
    if sha256(raw).hexdigest() != manifest.get("normalized_csv_sha256"):
        raise HistoricalArchiveError("normalized historical archive digest mismatch")
    if artifact_store is not None:
        _verify_artifact_evidence(
            store=artifact_store,
            manifest=manifest,
            normalized_csv=raw,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HistoricalArchiveError("normalized CSV is not UTF-8") from exc
    from io import StringIO
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames != ["open_time_utc", "open", "high", "low", "close", "volume"]:
        raise HistoricalArchiveError("historical CSV header differs from canonical")
    prices = []
    dates = []
    candles = []
    for row in reader:
        if any(type(v) is not str for v in row.values()):
            raise HistoricalArchiveError("historical CSV row malformed")
        try:
            point = datetime.fromisoformat(row["open_time_utc"].replace("Z", "+00:00"))
            if point.tzinfo != timezone.utc:
                raise ValueError("timezone mismatch")
            for n in ("open", "high", "low", "close", "volume"):
                v = Decimal(row[n])
                if not v.is_finite() or v < 0 or (n != "volume" and v == 0):
                    raise ValueError("nonpositive OHLC or nonfinite amount")
            low, high = Decimal(row["low"]), Decimal(row["high"])
            if low > min(Decimal(row["open"]), Decimal(row["close"])) or high < max(Decimal(row["open"]), Decimal(row["close"])):
                raise ValueError("invalid OHLC order")
        except (InvalidOperation, TypeError, ValueError, KeyError) as exc:
            raise HistoricalArchiveError("malformed candle in normalized archive") from exc
        dates.append(point)
        prices.append(row["close"])
        candles.append(tuple(row[key] for key in ("open", "high", "low", "close", "volume")))
        if len(prices) > 10000:
            raise HistoricalArchiveError("one canonical ZERO run is capped at 10000 observations")
    if len(prices) != manifest.get("row_count"):
        raise HistoricalArchiveError("candle rows differ from verified manifest")
    if not prices or dates[0].isoformat().replace("+00:00", "Z") != manifest.get("start_open_time_utc") or dates[-1].isoformat().replace("+00:00", "Z") != manifest.get("end_open_time_utc"):
        raise HistoricalArchiveError("time range conflicts with manifest")
    step = INTERVAL_SECONDS[interval]
    if any(int((b-a).total_seconds()) != step for a,b in zip(dates, dates[1:])):
        raise HistoricalArchiveError("internal timestamp gap/reorder")
    # Recalculate UTC-calendar completeness from actual rows, not from
    # self-reported manifest fields. A truncated first/last bar and a locally
    # recomputed CSV hash must never silently qualify a partial month.
    from calendar import monthrange
    year, month_number = map(int, month.split("-"))
    expected_first = (_utc_day(day) if day is not None else
                      datetime(year, month_number, 1, tzinfo=timezone.utc))
    expected_bars = (1 if day is not None else monthrange(year, month_number)[1]) * 86400 // step
    expected_last = expected_first + timedelta(seconds=step * (expected_bars - 1))
    if len(dates) != expected_bars or dates[0] != expected_first or dates[-1] != expected_last:
        raise HistoricalArchiveError("historical coverage does not include full UTC month")
    return dates, (tuple(candles) if include_ohlcv else prices), manifest


def load_verified_months(
    root: Path,
    symbol: str,
    interval: str,
    from_month: str,
    through_month: str,
    *,
    artifact_store: ArtifactStore | None = None,
):
    if SYMBOL.fullmatch(symbol) is None or interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("invalid instrument selection")
    all_dates, all_prices, digests = [], [], []
    step = INTERVAL_SECONDS[interval]
    months = list(_month_range(from_month, through_month))
    for month in months:
        dates, prices, manifest = _checked_month(
            root,
            symbol,
            interval,
            month,
            artifact_store=artifact_store,
        )
        if all_dates and dates[0] - all_dates[-1] != timedelta(seconds=step):
            raise HistoricalArchiveError("missing UTC time between adjacent monthly sources")
        all_dates.extend(dates)
        all_prices.extend(prices)
        digests.append({"month": month, "source_zip_sha256": manifest["source_zip_sha256"],
                        "normalized_csv_sha256": manifest["normalized_csv_sha256"]})
        if len(all_prices) > 10000:
            raise HistoricalArchiveError("over 10000 bars; use shorter separate *non-continuous* research runs")
    digest = sha256(json.dumps(
        {"market": "spot", "symbol": symbol, "interval": interval, "sources": digests,
         "observation_count": len(all_prices)}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return all_dates, all_prices, digest


def _historical_execution_plan(
    *,
    dates,
    candles,
    interval_seconds: int,
    target_quantity,
    instrument,
    dataset_sha256: str,
    fee_rate,
    maximum_participation,
    slippage_bps,
    half_spread_bps,
):
    """Project immutable bars; the canonical simulator remains fill authority."""

    from mvp.autotrade_mvp.exact_decimal import exact_multiply
    from mvp.autotrade_mvp.execution_realism import SimulatedOrder
    from mvp.autotrade_mvp.persistence import payload_digest
    from tools.historical_execution_scenarios import (
        conservative_bar_model,
        evaluate_completed_next_bar,
    )

    model = conservative_bar_model(
        model_version="plan10-historical-adverse-v1",
        calibration_sha256=dataset_sha256,
        instrument=instrument,
        fee_rate=fee_rate,
        maximum_participation=maximum_participation,
        slippage_bps=slippage_bps,
        half_spread_bps=half_spread_bps,
    )
    plan = []
    for index, (opened_at, candle) in enumerate(zip(dates[1:], candles[1:]), 1):
        completed_at = opened_at + timedelta(seconds=interval_seconds)
        decision_time = opened_at.isoformat().replace("+00:00", "Z")
        completed_text = completed_at.isoformat().replace("+00:00", "Z")
        open_price, high_price, low_price, close_price, volume = candle
        results = {}
        for side in ("BUY", "SELL"):
            order = SimulatedOrder.create(
                order_id=f"historical-plan-{index}-{side.lower()}",
                instrument_version=(
                    f"{instrument.instrument_id}@{instrument.version}"
                ),
                side=side,
                order_type="MARKET",
                quantity=target_quantity,
                submitted_at=decision_time,
                lot_size=instrument.quantity_step,
            )
            result = evaluate_completed_next_bar(
                order,
                model,
                as_of=completed_text,
                interval_start=decision_time,
                interval_end=completed_text,
                available_at=completed_text,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                base_volume=volume,
            )
            if result.status not in {"FILLED", "PARTIAL", "NO_FILL"}:
                raise HistoricalArchiveError(
                    "historical execution is ambiguous and cannot mutate financial state"
                )
            if result.fill_price is not None and result.fee != exact_multiply(
                result.filled_quantity,
                result.fill_price,
                model.fee_rate,
            ):
                raise HistoricalArchiveError(
                    "historical execution fee conflicts with the canonical provider rate"
                )
            results[side] = result
        buy, sell = results["BUY"], results["SELL"]
        expected_trade_time = (
            None if buy.filled_quantity == 0 else completed_text
        )
        if (
            buy.status != sell.status
            or buy.filled_quantity != sell.filled_quantity
            or buy.trade_time != expected_trade_time
            or sell.trade_time != expected_trade_time
            or buy.evidence_available_at != completed_text
            or sell.evidence_available_at != completed_text
            or buy.model_fingerprint != sell.model_fingerprint
        ):
            raise HistoricalArchiveError(
                "historical buy/sell execution capacity or chronology conflicts"
            )
        bar = {
            "interval_start": decision_time,
            "interval_end": completed_text,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }
        plan.append({
            "decision_time": decision_time,
            "completed_at": completed_text,
            "available_at": completed_text,
            "capacity_quantity": format(buy.filled_quantity, "f"),
            "capacity_status": buy.status,
            "buy_price": None if buy.fill_price is None else format(buy.fill_price, "f"),
            "sell_price": None if sell.fill_price is None else format(sell.fill_price, "f"),
            "model_fingerprint": buy.model_fingerprint,
            "bar_sha256": payload_digest(bar),
        })
    return plan, model.fingerprint


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a frozen, unqualified candle-close ZERO experiment")
    parser.add_argument("--data-root", default="historical-data")
    parser.add_argument(
        "--artifact-store",
        default=None,
        help=("canonical evidence store; defaults to "
              "<data-root>/.artifact-store and is mandatory for CLI replay"),
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", choices=tuple(INTERVAL_SECONDS), default="1h")
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", required=True)
    parser.add_argument("--state-dir", default=None,
                        help="separate durable directory per virtual account; defaults are budget-isolated")
    parser.add_argument("--run-id", default="research-2024")
    parser.add_argument("--virtual-cash", choices=("200", "1000"), default="1000")
    parser.add_argument("--stop-after-episodes", type=int)
    parser.add_argument("--next-bar-open", action="store_true",
                        help="decision on a closed bar, simulated fill at following bar open with fixed adverse costs")
    parser.add_argument("--slippage-bps", default="10",
                        help="next-bar adverse slippage basis points (0..100)")
    parser.add_argument("--half-spread-bps", default="5",
                        help="next-bar adverse half-spread basis points (0..100)")
    parser.add_argument("--maximum-participation", default="0.25",
                        help="maximum fraction of next-bar base volume (0..1)")
    parser.add_argument("--model-path", default=None,
                        help="verified trained model from an earlier, disjoint historical period; requires --next-bar-open")
    parser.add_argument("--validation-report-path", default=None,
                        help="verified disjoint validation evidence ending before this holdout; requires --model-path")
    parser.add_argument("--candidate-fast", type=int, default=None,
                        help="research-only registered MA candidate; requires --candidate-slow and --next-bar-open")
    parser.add_argument("--candidate-slow", type=int, default=None,
                        help="research-only registered MA candidate; requires --candidate-fast and --next-bar-open")
    parser.add_argument(
        "--strategy-plan", default=None,
        help=("content-addressed deterministic BUY/SELL/HOLD plan derived from "
              "causal closed bars; requires --next-bar-open and no trained/MA candidate"),
    )
    args = parser.parse_args(argv)
    try:
        data_root = Path(args.data_root)
        artifact_root = (Path(args.artifact_store) if args.artifact_store is not None
                         else data_root / ".artifact-store")
        _reject_symlink_ancestors(artifact_root)
        from tools.historical_causal_replay import (
            BAR_CLOSE_SCENARIO, closed_candle_simulation_input,
        )
        dates, opens, prices, digest = closed_candle_simulation_input(
            data_root, symbol=args.symbol, interval=args.interval,
            from_month=args.from_month, through_month=args.through_month,
            artifact_store=ArtifactStore(artifact_root),
        )
        # In next-open mode the specialist sees closed bars [0..n-2], while
        # only the canonical execution authority sees next-bar opens [1..n-1].
        # Both cuts are immutable and bound into the restart protocol.
        if args.next_bar_open and len(prices) < 2:
            raise HistoricalArchiveError("next-bar simulation needs at least two complete candles")
        if args.next_bar_open:
            slippage = Decimal(args.slippage_bps)
            half_spread = Decimal(args.half_spread_bps)
            participation = Decimal(args.maximum_participation)
            if (
                not slippage.is_finite()
                or not Decimal("0") <= slippage <= Decimal("100")
                or not half_spread.is_finite()
                or not Decimal("0") <= half_spread <= Decimal("100")
                or not participation.is_finite()
                or not Decimal("0") <= participation <= Decimal("1")
            ):
                raise HistoricalArchiveError(
                    "historical spread/slippage/participation configuration is invalid"
                )
        elif args.slippage_bps != "10":
            raise HistoricalArchiveError("slippage configuration requires --next-bar-open")
        elif args.half_spread_bps != "5" or args.maximum_participation != "0.25":
            raise HistoricalArchiveError(
                "spread/liquidity configuration requires --next-bar-open"
            )
        if args.model_path is not None and not args.next_bar_open:
            raise HistoricalArchiveError("trained model requires --next-bar-open")
        if (args.candidate_fast is None) != (args.candidate_slow is None):
            raise HistoricalArchiveError("candidate fast/slow parameters must be provided together")
        if args.candidate_fast is not None:
            from mvp.autotrade_mvp.adaptive_ma import EXPERTS
            if (
                not args.next_bar_open
                or args.model_path is not None
                or (args.candidate_fast, args.candidate_slow) not in EXPERTS
            ):
                raise HistoricalArchiveError(
                    "research candidate parameters require --next-bar-open, no model, and a registered pair"
                )
        if args.strategy_plan is not None and (
            not args.next_bar_open
            or args.model_path is not None
            or args.validation_report_path is not None
            or args.candidate_fast is not None
        ):
            raise HistoricalArchiveError(
                "registered deterministic strategy plan requires next-bar mode without another model"
            )
        model_fast, model_slow, model_digest = 2, 3, None
        model_schema = None
        simulation_model_digest = None
        if args.model_path is not None:
            from tools.train_historical_agent import read_model
            from mvp.autotrade_mvp.adaptive_ma import verify_model
            trained_model = read_model(Path(args.model_path))
            model_fast, model_slow, model_digest = verify_model(
                trained_model, symbol=args.symbol,
                interval=args.interval, holdout_start_open=dates[0],
            )
            model_schema = trained_model["schema"]
            simulation_model_digest = model_digest
        elif args.candidate_fast is not None:
            model_fast, model_slow = args.candidate_fast, args.candidate_slow
            simulation_model_digest = sha256(json.dumps({
                "schema": "autotrade-research-dag-candidate-v1",
                "fast": model_fast, "slow": model_slow,
                "dataset_identity_sha256": digest,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        validation_digest = None
        if args.validation_report_path is not None:
            if model_digest is None:
                raise HistoricalArchiveError("validation report requires a trained model")
            from mvp.autotrade_mvp.adaptive_ma import verify_validation_report
            validation_digest = verify_validation_report(
                read_model(Path(args.validation_report_path)),
                model_sha256=model_digest, symbol=args.symbol,
                interval=args.interval, holdout_start_open=dates[0],
                selected_parameters=(model_fast, model_slow),
            )
        execution_prices = opens[1:] if args.next_bar_open else prices
        signal_prices = prices[:-1] if args.next_bar_open else None
        strategy_plan = None
        if args.strategy_plan is not None:
            strategy_plan = _read_strategy_plan(
                Path(args.strategy_plan),
                dataset_identity_sha256=digest,
                symbol=args.symbol,
                interval=args.interval,
                observation_count=len(execution_prices),
                signal_prices=signal_prices,
                available_at=dates[1:],
            )
        first_execution_time = (dates[1] if args.next_bar_open else
                                dates[0] + timedelta(seconds=INTERVAL_SECONDS[args.interval]))
        # The first CLOSED candle is known at first_close. Freeze a bounded,
        # affordable SPOT quantity from that observation alone; no future
        # bars, result labels or hindsight portfolio figures are consulted.
        # Existing canonical ZERO risk/OMS/accounting still own every fill.
        first_closed_price = Decimal(prices[0])
        research_notional = Decimal(args.virtual_cash) * Decimal("0.25")
        research_target = (research_notional / first_closed_price).quantize(
            Decimal("0.00000001"), rounding=ROUND_DOWN
        )
        if research_target <= 0:
            raise HistoricalArchiveError("first closed price exceeds bounded fractional research quantity")
        historical_execution_plan = None
        execution_model_sha256 = None
        if args.next_bar_open:
            execution_dates, execution_candles = [], []
            for month in _month_range(args.from_month, args.through_month):
                month_dates, month_candles, _manifest = _checked_month(
                    data_root,
                    args.symbol,
                    args.interval,
                    month,
                    artifact_store=ArtifactStore(artifact_root),
                    include_ohlcv=True,
                )
                execution_dates.extend(month_dates)
                execution_candles.extend(month_candles)
            if execution_dates != dates:
                raise HistoricalArchiveError(
                    "historical execution bars differ from the causal feeder cut"
                )
            from mvp.autotrade_mvp.simulation_session import (
                FEE_RATE,
                autonomous_simulation_instrument,
            )
            canonical_instrument = autonomous_simulation_instrument(
                dates[1],
                "FRACTIONAL_SPOT_RESEARCH",
            )
            historical_execution_plan, execution_model_sha256 = _historical_execution_plan(
                dates=execution_dates,
                candles=execution_candles,
                interval_seconds=INTERVAL_SECONDS[args.interval],
                target_quantity=research_target,
                instrument=canonical_instrument,
                dataset_sha256=digest,
                fee_rate=FEE_RATE,
                maximum_participation=args.maximum_participation,
                slippage_bps=args.slippage_bps,
                half_spread_bps=args.half_spread_bps,
            )
        state_dir = args.state_dir or (
            "historical-zero-state" if args.virtual_cash == "1000"
            else "historical-zero-state-200"
        )
        from tools.historical_risk_context import HistoricalRiskGate
        historical_risk = HistoricalRiskGate(
            state_dir=state_dir,
            run_id=args.run_id + ("-nextopen-" if args.next_bar_open else "-") + digest[:16],
            execution_prices=execution_prices,
            signal_prices=signal_prices if signal_prices is not None else execution_prices,
            execution_times=[
                first_execution_time + timedelta(seconds=index * INTERVAL_SECONDS[args.interval])
                for index in range(len(execution_prices))
            ],
            initial_cash=args.virtual_cash,
            target_quantity=format(research_target, "f"),
        )
        result = run_autonomous_simulation(
            execution_prices, state_dir,
            run_id=args.run_id + ("-nextopen-" if args.next_bar_open else "-") + digest[:16],
            now=first_execution_time.isoformat().replace("+00:00", "Z"),
            observation_interval_seconds=INTERVAL_SECONDS[args.interval],
            stop_after_episodes=args.stop_after_episodes,
            instrument_profile="FRACTIONAL_SPOT_RESEARCH",
            target_quantity=format(research_target, "f"),
            virtual_cash=args.virtual_cash,
            **({"signal_prices": signal_prices, "slippage_bps": args.slippage_bps}
               if args.next_bar_open else {}),
            **({"historical_execution_plan": historical_execution_plan}
               if historical_execution_plan is not None else {}),
            strategy_fast=model_fast, strategy_slow=model_slow,
            model_sha256=simulation_model_digest,
            validation_sha256=validation_digest,
            **({
                "strategy_side_plan": strategy_plan["side_plan"],
                "strategy_plan_sha256": strategy_plan["plan_sha256"].removeprefix(
                    "sha256:"
                ),
            } if strategy_plan is not None else {}),
            should_pause=historical_risk,
        )
        comparative = None
        if args.next_bar_open:
            from tools.historical_holdout_report import holdout_report
            comparative = holdout_report(
                result, execution_prices=execution_prices,
                execution_dates=dates[1:], virtual_cash=args.virtual_cash,
                slippage_bps=args.slippage_bps,
            )
        print(json.dumps({
            "label": ("INTERNAL_NEXT_BAR_OPEN_SCENARIO_NOT_PERFORMANCE_EVIDENCE"
                      if args.next_bar_open else
                      "INTERNAL_CANDLE_CLOSE_SAME_PRICE_EXPERIMENT_NOT_PERFORMANCE_EVIDENCE"),
            "source_market": "BINANCE_HISTORICAL_SPOT_CANDLES",
            "original_symbol": args.symbol,
            "historical_start_utc": dates[0].isoformat().replace("+00:00", "Z"),
            "historical_end_utc": dates[-1].isoformat().replace("+00:00", "Z"),
            "observation_count": len(prices),
            "execution_count": len(execution_prices),
            "execution_timing": ("PRIOR_CLOSED_BAR_TO_NEXT_COMPLETED_AVAILABLE_BAR" if args.next_bar_open
                                 else "SAME_OBSERVED_CLOSE_EXPERIMENT"),
            "adverse_slippage_bps": args.slippage_bps if args.next_bar_open else "0",
            "adverse_half_spread_bps": args.half_spread_bps if args.next_bar_open else "0",
            "maximum_bar_participation": (
                args.maximum_participation if args.next_bar_open else "0"
            ),
            "execution_model_sha256": execution_model_sha256,
            "trained_model_sha256": model_digest,
            "trained_model_schema": model_schema,
            "research_candidate_config_sha256": (
                simulation_model_digest if args.candidate_fast is not None else None
            ),
            "registered_strategy_plan_sha256": (
                None if strategy_plan is None else strategy_plan["plan_sha256"]
            ),
            "registered_strategy_candidate": (
                None if strategy_plan is None else strategy_plan["candidate"]
            ),
            "registered_strategy_quantity_semantics": (
                None if strategy_plan is None else
                "PROPOSAL_METADATA_ONLY; CANONICAL_ZERO_OWNS_EXECUTION_TARGET"
            ),
            "validation_report_sha256": validation_digest,
            "trained_strategy_parameters": {"fast": model_fast, "slow": model_slow},
            "dataset_identity_sha256": digest,
            "availability_policy": BAR_CLOSE_SCENARIO,
            "frozen_fractional_spot_target_quantity": format(research_target, "f"),
            "target_sizing_basis": (
                f"USD {research_notional} (25% of selected virtual cash) / first completed "
                "candle close, rounded down to 0.00000001; remaining virtual cash stays unspent"
            ),
            "virtual_cash_usd": args.virtual_cash,
            "important_limitations": [
                ("Registered deterministic strategy decisions were frozen from causal closed-bar prefixes; this evaluation run cannot change them."
                 if strategy_plan is not None else
                 "Selected moving-average parameters were updated only from earlier CLOSED reconciled simulation position cycles; this holdout run does not update them."
                 if model_schema == "autotrade-closed-simulation-training-v2" else
                 "Selected moving-average parameters use a legacy earlier closed-bar directional diagnostic; this holdout run does not update them."
                 if model_digest is not None else
                 "Existing agent is currently a frozen moving-average strategy; this run does not prove learning."),
                ("This run evaluates parameters from a separate training cut; no holdout updates are permitted."
                 if model_digest is not None else "No trained model is selected for this run."),
                ("Next-bar mode applies adverse high/low, spread, slippage, fees and bounded OHLCV participation; it is not measured order-book liquidity."
                 if args.next_bar_open else
                 "Existing engine executes at observed mark, not qualified next-bar open/spread/slippage."),
                "Bar-close availability is a retrospective scenario, not proof that these archive bytes were published at that historical time.",
                "OHLCV has no order-book or tick-level execution truth.",
                f"Virtual {args.virtual_cash} USD, zero network orders. Do not infer profitability from these fills.",
            ],
            "simulation": result,
            "historical_risk": historical_risk.report(),
            "comparative_report": comparative,
        }, indent=2))
        return 2 if result.get("status") == "UNKNOWN" else 0
    except (HistoricalArchiveError, OSError, RuntimeError, ValueError, TypeError, ArithmeticError) as exc:
        print(f"AutoTrade historical ZERO experiment blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
