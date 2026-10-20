"""One shared cost-aware forecast packet for the AutoTrade agent team.

Run after a deliberate training/refresh trigger, NOT once per market tick or per
LLM agent. The same immutable packet can be read by every specialist. This module
has no broker calls and is not wired to the financial host automatically.
"""
from __future__ import annotations

from hashlib import sha256
import json

from .alpha_economics import AlphaInputError, CostBudget, opportunity
from .alpha_models import BACKENDS, PriceSeries, forecast_suite


def build_alpha_packet(series: PriceSeries, costs: CostBudget, *, lookback=24,
                       horizon=1, steps=32, backends=BACKENDS, seed=7,
                       model_artifacts=None) -> dict:
    if type(costs) is not CostBudget or type(series) is not PriceSeries:
        raise AlphaInputError('canonical research records required')
    costs.validate(series.asof_ms)
    if series.market.split('_')[-1] != costs.quote_currency:
        raise AlphaInputError('model-cost conversion to quote currency required')
    forecast = forecast_suite(series, lookback=lookback, horizon=horizon,
                              steps=steps, backends=backends, seed=seed,
                              model_artifacts=model_artifacts)
    opportunities = {}
    for name, model in forecast['models'].items():
        opportunities[name] = opportunity(model['gross_midpoint_return_ratio'],
                                          model['lower_return_ratio'], costs,
                                          market=series.market, asof_ms=series.asof_ms)
    # Same-model outputs are not four independent votes. Ensemble was fixed
    # before holdout evaluation; automatic champion selection is intentionally absent.
    result = {'schema_version': '1.0', 'forecast': forecast, 'opportunities': opportunities,
              'coordination': {'input_snapshot_id': series.snapshot_id,
                               'correlation_group': 'SAME_PRICE_HISTORY',
                               'refresh_policy': 'EXPLICIT_OFFLINE_RESEARCH_TRIGGER',
                               'broker_keys_exposed': False, 'independent_votes': False,
                               'next_boundary': 'CANONICAL_EVIDENCE_STORE_AND_SPECIALIST_DAG'},
              'live_authority': False, 'automatic_promotion': False}
    body = json.dumps(result, sort_keys=True, allow_nan=False, separators=(',', ':'))
    result['packet_sha256'] = sha256(body.encode()).hexdigest()
    return result
