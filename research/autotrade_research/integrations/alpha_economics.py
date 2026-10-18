"""WhiteBIT research economics. No credentials, network, orders or ledger writes.

Costs are explicit assumptions, not verified fills. Monetary authority remains in
AutoTrade's existing exact ledger/risk. All return inputs use dimensionless ratios.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import re
from typing import Any


class AlphaInputError(ValueError):
    pass


def exact(value: Any, name: str, *, low: str = '-1000000', high: str = '1000000') -> Fraction:
    if type(value) not in (str, int, Decimal):
        raise AlphaInputError(f'{name}: exact decimal string/integer required')
    if isinstance(value, str) and len(value) > 80:
        raise AlphaInputError(f'{name}: excessive numeric input')
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise AlphaInputError(f'{name}: invalid decimal') from None
    if (not d.is_finite() or len(d.as_tuple().digits) > 32 or
            abs(d.as_tuple().exponent) > 18 or not Decimal(low) <= d <= Decimal(high)):
        raise AlphaInputError(f'{name}: outside numeric envelope')
    return Fraction(d)


def decimal_text(value: Fraction) -> str:
    with localcontext() as c:
        c.prec = 60
        return format(Decimal(value.numerator) / Decimal(value.denominator), '.24f').rstrip('0').rstrip('.') or '0'


def market_name(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r'[A-Z0-9]{1,20}_[A-Z0-9]{1,20}', value):
        raise AlphaInputError('invalid WhiteBIT market')
    return value


def timestamp(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value < 2**63:
        raise AlphaInputError(f'{name}: invalid timestamp')
    return value


def fee_ratios(payload: dict, market: str, *, product: str) -> dict[str, str]:
    """Parse POST /api/v4/market/fee, whose rates are percent, not fractions.

    /api/v4/public/fee is deposit/withdrawal metadata, NOT this trading schema.
    Per-market overrides take precedence. RPI orders are not supported here.
    A missing malformed override is never silently replaced by the default.
    """
    market_name(market)
    if type(product) is not str or product not in ('SPOT', 'FUTURES'):
        raise AlphaInputError('explicit SPOT/FUTURES product required')
    if type(payload) is not dict or payload.get('error') is not None:
        raise AlphaInputError('invalid trading-fee payload')
    custom = payload.get('custom_fee', {})
    if type(custom) is not dict:
        raise AlphaInputError('custom_fee must be a dictionary')
    if market in custom:
        selected = custom[market]
        if type(selected) is not dict:
            raise AlphaInputError('invalid market fee override')
        keys = ('maker', 'taker')
    else:
        selected = payload
        prefix = '' if product == 'SPOT' else 'futures_'
        keys = (prefix + 'maker', prefix + 'taker')
    result = {}
    for label, key in zip(('maker', 'taker'), keys):
        if key not in selected:
            raise AlphaInputError(f'missing {key} trading fee')
        result[label] = decimal_text(exact(selected[key], key, low='0', high='10') / 100)
    return {**result, 'market': market, 'product': product, 'units': 'RATIO',
            'source_route': '/api/v4/market/fee'}


@dataclass(frozen=True)
class CostBudget:
    """First-order round-trip cost assumptions, all expressed in quote currency.

    A basis point is 0.0001 of traded notional. Spread is the full round-trip
    bid/ask crossing cost; slippage excludes it. Funding/borrow use the intended
    holding horizon, not a blindly annualized current rate. Rebates/revenue are
    excluded from this conservative cost envelope.
    """
    entry_fee_ratio: str
    exit_fee_ratio: str
    spread_bps: str
    entry_slippage_bps: str
    exit_slippage_bps: str
    funding_borrow_bps: str
    model_cost_quote: str
    notional_quote: str
    min_notional_quote: str
    quote_currency: str
    source_ref: str
    observed_at_ms: int
    valid_until_ms: int

    def validate(self, asof_ms: int) -> None:
        timestamp(asof_ms, 'asof_ms')
        t = timestamp(self.observed_at_ms, 'observed_at_ms')
        end = timestamp(self.valid_until_ms, 'valid_until_ms')
        if not t <= asof_ms <= end:
            raise AlphaInputError('future or stale cost assumptions')
        for field in ('entry_fee_ratio', 'exit_fee_ratio'):
            exact(getattr(self, field), field, low='0', high='0.1')
        for field in ('spread_bps', 'entry_slippage_bps', 'exit_slippage_bps', 'funding_borrow_bps'):
            exact(getattr(self, field), field, low='0', high='10000')
        exact(self.model_cost_quote, 'model_cost_quote', low='0')
        if exact(self.notional_quote, 'notional_quote', low='0') <= 0:
            raise AlphaInputError('positive notional required')
        if exact(self.min_notional_quote, 'min_notional_quote', low='0') <= 0:
            raise AlphaInputError('positive verified/explicit minimum notional required')
        if type(self.quote_currency) is not str or not re.fullmatch('[A-Z0-9]{1,20}', self.quote_currency):
            raise AlphaInputError('explicit quote currency required')
        if type(self.source_ref) is not str or not 1 <= len(self.source_ref) <= 512:
            raise AlphaInputError('cost source/evidence reference required')

    def variable_ratio(self) -> Fraction:
        fees = exact(self.entry_fee_ratio, 'entry fee', low='0', high='0.1') + exact(self.exit_fee_ratio, 'exit fee', low='0', high='0.1')
        extra = sum((exact(getattr(self, k), k, low='0', high='10000') for k in (
            'spread_bps', 'entry_slippage_bps', 'exit_slippage_bps', 'funding_borrow_bps')), Fraction()) / 10000
        return fees + extra

    def total_ratio(self) -> Fraction:
        return self.variable_ratio() + exact(self.model_cost_quote, 'model cost', low='0') / exact(self.notional_quote, 'notional', low='0')


def opportunity(gross_return_ratio: str, lower_return_ratio: str, cost: CostBudget, *,
                market: str, asof_ms: int) -> dict:
    """A long-only research candidate; not a financial admission decision."""
    market_name(market)
    if type(cost) is not CostBudget:
        raise AlphaInputError('CostBudget required')
    cost.validate(asof_ms)
    if market.split('_')[-1] != cost.quote_currency:
        raise AlphaInputError('market and cost quote currency differ')
    gross = exact(gross_return_ratio, 'gross midpoint return', low='-1', high='10')
    lower = exact(lower_return_ratio, 'lower midpoint return', low='-1', high='10')
    if lower > gross:
        raise AlphaInputError('lower return exceeds central forecast')
    total = cost.total_ratio()
    notional = exact(cost.notional_quote, 'notional', low='0')
    minimum = exact(cost.min_notional_quote, 'min notional', low='0')
    reason = ('BELOW_MINIMUM_NOTIONAL' if notional < minimum else
              'NO_EDGE_AFTER_COSTS' if gross <= total else
              'UNCERTAINTY_EXCEEDS_EDGE' if lower <= total else 'POSITIVE_RESEARCH_EDGE')
    return {'market': market, 'mode': 'SHADOW_RESEARCH_ONLY',
            'candidate': reason == 'POSITIVE_RESEARCH_EDGE', 'reason': reason,
            'estimated_net_return_ratio': decimal_text(gross-total),
            'lower_net_return_ratio': decimal_text(lower-total),
            'round_trip_cost_bps': decimal_text(total*10000),
            'estimated_quote_gain': decimal_text((gross-total)*notional),
            'quote_currency': cost.quote_currency, 'cost_source_ref': cost.source_ref,
            'cost_model': 'FIRST_ORDER_NOTIONAL_APPROXIMATION',
            'financial_admission': 'NOT_PERFORMED', 'live_authority': False,
            'profitability_proven': False}


def leverage_scenarios(gross_return_ratio: str, cost: CostBudget, *, equity_quote: str,
                       leverage_values: tuple[int, ...], asof_ms: int) -> tuple[dict, ...]:
    """Linear sensitivity, not a liquidation model or a suggested leverage setting.

    Cost budget is assumed calibrated at its stated notional. A larger scenario
    generally changes impact/fees; every result requires recalibration. There is
    no prediction of WhiteBIT maintenance margin or ability to close a position.
    """
    if type(cost) is not CostBudget:
        raise AlphaInputError('CostBudget required')
    cost.validate(asof_ms)
    equity = exact(equity_quote, 'equity', low='0')
    if equity <= 0:
        raise AlphaInputError('positive equity required')
    if type(leverage_values) is not tuple or not 1 <= len(leverage_values) <= 10:
        raise AlphaInputError('bounded leverage tuple required')
    if len(set(leverage_values)) != len(leverage_values):
        raise AlphaInputError('duplicate leverage scenario')
    gross = exact(gross_return_ratio, 'return', low='-1', high='10')
    overhead = exact(cost.model_cost_quote, 'overhead', low='0')
    rows = []
    for leverage in leverage_values:
        if type(leverage) is not int or not 1 <= leverage <= 20:
            raise AlphaInputError('scenario leverage must be 1..20')
        pnl = equity*leverage*(gross-cost.variable_ratio())-overhead
        rows.append({'leverage': leverage, 'estimated_quote_pnl': decimal_text(pnl),
                     'estimated_equity_return_ratio': decimal_text(pnl/equity),
                     'equity_exhausted_in_linear_scenario': pnl <= -equity,
                     'liquidation_model': 'NOT_MODELED', 'impact_recalibration_required': True,
                     'position_enabled': False})
    return tuple(rows)
