"""Fast research screening over verified archives, without the ZERO bar cap."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
import json

from research.autotrade_research.strategies.fast_screen import Costs, QuickReject, digest, screen
from tools.train_historical_agent import save_model
from tools.historical_public_klines import _reject_symlink_ancestors


def stage_checkpoint(directory):
    directory = Path(directory)
    _reject_symlink_ancestors(directory)

    def run(key, producer):
        path = directory / (key + ".json")
        _reject_symlink_ancestors(path)
        if path.exists():
            if not path.is_file() or path.stat().st_size > 100000:
                raise ValueError("screening stage checkpoint is not bounded")
            from research.autotrade_research.io.strict_json import strict_json_loads
            record = strict_json_loads(path.read_text(encoding="utf-8"))
            if (type(record) is not dict or set(record) != {"key", "result", "sha256"}
                    or record["key"] != key or record["sha256"] != digest(record["result"])):
                raise ValueError("screening stage checkpoint identity differs")
            return record["result"]
        result = producer()
        save_model(path, dict(key=key, result=result, sha256=digest(result)))
        return result
    return run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--from-month", default="2024-01")
    parser.add_argument("--through-month", default="2024-03")
    parser.add_argument("--train-count", required=True, type=int)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--synthetic-bars", type=int, default=0,
                        help="explicit deterministic benchmark only; no market evidence")
    for field in ("initial_cash", "fee_bps", "spread_bps", "slippage_bps", "capital_fraction", "threshold"):
        parser.add_argument("--" + field.replace("_", "-"), default=getattr(Costs(), field))
    parser.add_argument("--lookback", type=int, default=8)
    parser.add_argument("--quick-reject-lookback", type=int, default=QuickReject().lookback)
    for field in ("min_volatility_fraction", "max_volatility_fraction",
                  "min_direction_agreement", "min_quote_volume"):
        parser.add_argument("--quick-reject-" + field.replace("_", "-"),
                            default=getattr(QuickReject(), field))
    args = parser.parse_args(argv)
    started = perf_counter()
    if args.synthetic_bars:
        if not 8 <= args.synthetic_bars <= 1000000:
            raise ValueError("synthetic benchmark must contain 8..1000000 bars")
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = [dict(opened_at=start+timedelta(minutes=i),
                     closed_at=start+timedelta(minutes=i+1),
                     open=str(100 + (i % 100)),
                     high=str(max(100 + (i % 100), 100 + ((i+1) % 100)) + 1),
                     low=str(min(100 + (i % 100), 100 + ((i+1) % 100)) - 1),
                     close=str(100 + ((i+1) % 100)), volume="100000")
                for i in range(args.synthetic_bars)]
        source = dict(kind="SYNTHETIC_BENCHMARK_ONLY", bars=len(bars), generator="sawtooth-v1")
        dataset_sha = digest(source)
    else:
        if not args.data_root:
            parser.error("--data-root is required for archive screening")
        from tools.historical_causal_replay import load_causal_archive, BAR_CLOSE_SCENARIO
        dataset = load_causal_archive(Path(args.data_root), symbol=args.symbol, interval=args.interval,
            from_month=args.from_month, through_month=args.through_month,
            availability_policy=BAR_CLOSE_SCENARIO)
        bars = [dict(opened_at=datetime.fromisoformat(event.payload["open_time_utc"]),
                     closed_at=event.event_time,
                     **{name: event.payload[name] for name in ("open", "high", "low", "close", "volume")})
                for event in dataset.events]
        dataset_sha = dataset.dataset_sha256.removeprefix("sha256:")
        source = dict(kind="VERIFIED_ARCHIVE_RETROSPECTIVE_SCENARIO", symbol=args.symbol,
                      interval=args.interval, from_month=args.from_month, through_month=args.through_month)
    costs = Costs(**{field: getattr(args, field) for field in Costs.__dataclass_fields__})
    quick_reject = QuickReject(**{
        field: getattr(args, "quick_reject_" + field) for field in QuickReject.__dataclass_fields__
    })
    output = Path(args.output_dir)
    result = screen(bars, train_count=args.train_count, dataset_sha256=dataset_sha,
                    costs=costs, quick_reject=quick_reject,
                    checkpoint=stage_checkpoint(output / "stages"))
    artifact = dict(source=source, result=result)
    target = output / (result["config_sha256"] + ".json")
    save_model(target, artifact)
    print(json.dumps(dict(result_path=str(target), observations=len(bars),
        elapsed_seconds=round(perf_counter()-started, 3), evidence_class=result["evidence_class"],
        selected_candidate=result["selected_candidate"], evaluation=result["evaluation"],
        result_sha256=result["result_sha256"], network_orders=False, paid_model_calls=0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
