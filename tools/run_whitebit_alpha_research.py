"""Bounded offline alpha research runner; no network, keys or trading route.

    python tools/run_whitebit_alpha_research.py --demo
    python tools/run_whitebit_alpha_research.py --input saved_whitebit_research.json

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
sys.path.insert(0, str(ROOT / 'research'))
from autotrade_research.integrations.alpha_economics import AlphaInputError, CostBudget
from autotrade_research.integrations.alpha_models import PriceSeries
from autotrade_research.integrations.alpha_packet import build_alpha_packet


def demo_input() -> dict:
    import numpy as np
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


def main(argv=None):
    parser = argparse.ArgumentParser(description='WhiteBIT offline research only — not live trading')
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument('--demo', action='store_true')
    src.add_argument('--input', type=Path)
    parser.add_argument('--steps', type=int, default=32)
    parser.add_argument('--horizon', type=int, default=1)
    args = parser.parse_args(argv)
    try:
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
        result = run_payload(payload, steps=args.steps, horizon=args.horizon)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
        return 0
    except (ValueError, TypeError, OSError, ImportError, RecursionError):
        print('ALPHA_RESEARCH_INPUT_OR_DEPENDENCY_ERROR; no financial operation executed', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
