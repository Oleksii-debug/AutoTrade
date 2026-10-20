"""Bounded offline alpha research runner; no network, keys or trading route.

    python tools/run_whitebit_alpha_research.py --demo
    python tools/run_whitebit_alpha_research.py --input saved_whitebit_research.json
    python tools/run_whitebit_alpha_research.py --input saved_whitebit_research.json --state-dir alpha-state
    python tools/run_whitebit_alpha_research.py --job-id UUID --state-dir alpha-state
    python tools/run_whitebit_alpha_research.py --input saved_whitebit_research.json --artifact-root alpha-artifacts

Input contains exactly 'series' and 'costs' matching PriceSeries/CostBudget. Prices,
fees and money are decimal strings; timestamps are integer milliseconds. Demo
creates synthetic prices and explicit hypothetical costs, NEVER real market data.
"""
from __future__ import annotations
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'research'))
from autotrade_research.integrations.alpha_economics import AlphaInputError, CostBudget


def demo_input() -> dict:
    import numpy as np
    from autotrade_research.integrations.alpha_models import PriceSeries
    t = np.arange(260)
    p = 100*np.exp(0.0002*t + 0.004*np.sin(t/7))
    end = int(t[-1])*60000 + 100
    series = PriceSeries('BTC_USDT', tuple(f'{v:.10f}' for v in p),
                         tuple(int(i)*60000 for i in t),
                         tuple(int(i)*60000+100 for i in t), 60000, end,
                         'SYNTHETIC_DEMO_NOT_WHITEBIT_MARKET_DATA', 'demo-only', 'SYNTHETIC_FIXTURE')
    costs = CostBudget('0.001', '0.001', '4', '1', '1', '0', '0.02', '100', '5',
                       'USDT', 'HYPOTHETICAL_NOT_ACCOUNT_FEES', end-1, end+1000)
    return {'series': asdict(series), 'costs': asdict(costs)}


def _unique_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise AlphaInputError('duplicate JSON key')
        out[k] = v
    return out


def run_payload(payload: dict, *, steps=32, horizon=1) -> dict:
    from autotrade_research.integrations.alpha_models import PriceSeries
    from autotrade_research.integrations.alpha_packet import build_alpha_packet

    if type(payload) is not dict or set(payload) != {'series', 'costs'}:
        raise AlphaInputError('expected series and costs only; no credential inputs')
    if type(payload['series']) is not dict or type(payload['costs']) is not dict:
        raise AlphaInputError('expected record dictionaries')
    values = dict(payload['series'])
    for key in ('closes', 'closed_at_ms', 'available_at_ms'):
        if type(values.get(key)) not in (tuple, list):
            raise AlphaInputError('expected series array')
        values[key] = tuple(values[key])
    return build_alpha_packet(PriceSeries(**values), CostBudget(**payload['costs']),
                              steps=steps, horizon=horizon)


def _durable_worker(state_dir: Path):
    from mvp.autotrade_mvp.alpha_research_worker import AlphaResearchWorker
    from research.autotrade_research.jobs import ResearchJobStore
    from research.autotrade_research.science.registry import ScientificRegistry
    artifact_root = state_dir / "artifacts"
    jobs = ResearchJobStore(state_dir / "jobs.sqlite3", authoritative_artifact_root=artifact_root)
    registry = ScientificRegistry(state_dir / "science.sqlite3")
    return AlphaResearchWorker(jobs=jobs, registry=registry, artifact_root=artifact_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description='WhiteBIT offline research only — not live trading')
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument('--demo', action='store_true')
    src.add_argument('--input', type=Path)
    src.add_argument('--packet-ref', help='Re-read one immutable packet receipt; no model refit')
    src.add_argument('--job-id', help='Continue or replay one durable alpha research job')
    parser.add_argument('--steps', type=int, default=32)
    parser.add_argument('--horizon', type=int, default=1)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--artifact-root', type=Path,
                        help='Canonical artifact directory for durable advisory DAG results')
    target.add_argument('--state-dir', type=Path,
                        help='Durable research jobs, native models and candidate registry directory')
    parser.add_argument('--snapshot-id', help='Expected input snapshot when re-reading a receipt')
    parser.add_argument('--asof-ms', type=int, help='Exact original research cutoff when re-reading a receipt')
    args = parser.parse_args(argv)
    if args.packet_ref and (args.artifact_root is None or args.snapshot_id is None or args.asof_ms is None):
        parser.error('--packet-ref requires --artifact-root, --snapshot-id and --asof-ms')
    if not args.packet_ref and (args.snapshot_id is not None or args.asof_ms is not None):
        parser.error('--snapshot-id and --asof-ms require --packet-ref')
    if args.job_id and args.state_dir is None:
        parser.error('--job-id requires --state-dir')
    try:
        if args.packet_ref:
            from mvp.autotrade_mvp.alpha_dag_binding import aggregate_alpha_receipt
            result = {'binding': aggregate_alpha_receipt(args.artifact_root, args.packet_ref,
                       expected_snapshot_id=args.snapshot_id, asof_ms=args.asof_ms)}
        elif args.job_id:
            worker = _durable_worker(args.state_dir)
            result = {'job_id': args.job_id, 'result': worker.run(args.job_id)}
        else:
            if args.demo:
                payload = demo_input()
            else:
                if not args.input.is_file() or args.input.stat().st_size > 4*1024*1024:
                    raise AlphaInputError('bounded regular input file required')
                with args.input.open('rb') as fh:
                    raw = fh.read(4*1024*1024+1)
                if len(raw) > 4*1024*1024:
                    raise AlphaInputError('excessive input')
                payload = json.loads(raw, object_pairs_hook=_unique_pairs,
                                     parse_constant=lambda _: (_ for _ in ()).throw(AlphaInputError('nonfinite JSON')))
            if args.state_dir is not None:
                worker = _durable_worker(args.state_dir)
                job, _created = worker.enqueue(payload, steps=args.steps, horizon=args.horizon)
                result = {'job_id': job['job_id'], 'result': worker.run(job['job_id'])}
            else:
                result = run_payload(payload, steps=args.steps, horizon=args.horizon)
                if args.artifact_root is not None:
                    from mvp.autotrade_mvp.alpha_dag_binding import publish_alpha_research
                    result = {'packet': result,
                              'binding': publish_alpha_research(args.artifact_root, payload, result)}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
        return 0
    except (ValueError, TypeError, KeyError, OSError, ImportError, RecursionError, RuntimeError):
        print('ALPHA_RESEARCH_INPUT_OR_DEPENDENCY_ERROR; no financial operation executed', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
