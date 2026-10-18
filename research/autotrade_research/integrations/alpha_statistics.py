"""Real statsmodels diagnostics shared by research specialists, not trade signals."""
from __future__ import annotations

from itertools import combinations
import warnings

import numpy as np
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.stattools import coint
from statsmodels.tools.sm_exceptions import CollinearityWarning

from .alpha_economics import AlphaInputError
from .alpha_models import PriceSeries


def adjust_hypotheses(p_values: tuple[float, ...], alpha: float = 0.05) -> dict:
    """Benjamini-Yekutieli adjustment: callers must retain ALL tried hypotheses.

    This does not repair invalid individual p-values, nonstationary residuals,
    repeated peeking or undisclosed strategy trials.
    """
    if type(p_values) is not tuple or not 1 <= len(p_values) <= 10000:
        raise AlphaInputError('bounded p-value tuple required')
    if any(type(p) not in (int, float) or not np.isfinite(p) or not 0 <= p <= 1 for p in p_values):
        raise AlphaInputError('invalid p-value')
    if type(alpha) not in (int, float) or not 0 < alpha < 1:
        raise AlphaInputError('invalid alpha')
    reject, adjusted, _, _ = multipletests(p_values, alpha=alpha, method='fdr_by')
    return {'method': 'fdr_by', 'adjusted_p_values': adjusted.tolist(),
            'rejected_null': reject.tolist(), 'trial_count': len(p_values),
            'profitability_proven': False}


def cointegration_scan(series: tuple[PriceSeries, ...], alpha: float = 0.05) -> dict:
    """Formation-period diagnostics for WhiteBIT pairs. No automatic pair trade.

    Each pair is evaluated in one declared direction only. Selecting whichever
    direction gives a better p-value would be an additional hypothesis.
    """
    if type(series) is not tuple or not 2 <= len(series) <= 8:
        raise AlphaInputError('two to eight aligned series required')
    for s in series:
        if type(s) is not PriceSeries:
            raise AlphaInputError('PriceSeries required')
    if len({s.market for s in series}) != len(series):
        raise AlphaInputError('duplicate markets')
    ref = series[0]
    logs = []
    for s in series:
        if (s.closed_at_ms != ref.closed_at_ms or s.asof_ms != ref.asof_ms or
                s.interval_ms != ref.interval_ms or s.market.split('_')[-1] != ref.market.split('_')[-1]):
            raise AlphaInputError('align times, horizon and quote currencies explicitly')
        values, _ = s.arrays()
        logs.append(np.log(values))
    pairs, p_values = [], []
    for i, j in combinations(range(len(series)), 2):
        a, b = logs[i], logs[j]
        degenerate = min(float(np.std(a)), float(np.std(b))) < 1e-10
        if degenerate:
            stat, p = None, 1.0
        else:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always', CollinearityWarning)
                statistic, p, _ = coint(a, b, trend='c', maxlag=8, autolag='aic')
            degenerate = any(issubclass(w.category, CollinearityWarning) for w in caught)
            if degenerate or not np.isfinite(statistic) or not np.isfinite(p):
                stat, p, degenerate = None, 1.0, True
            else:
                stat = float(statistic)
        p_values.append(float(p))
        pairs.append({'left': series[i].market, 'right': series[j].market,
                      'statistic': stat, 'p_value': float(p), 'degenerate': degenerate})
    corrected = adjust_hypotheses(tuple(p_values), alpha)
    for pair, p, reject in zip(pairs, corrected['adjusted_p_values'], corrected['rejected_null']):
        pair.update(adjusted_p_value=p, hypothesis_flag=bool(reject) and not pair['degenerate'])
    return {'pairs': pairs, 'method': 'Engle-Granger + Benjamini-Yekutieli',
            'asof_ms': ref.asof_ms, 'source_refs': [s.source_ref for s in series],
            'evidence_classes': [s.evidence_class for s in series],
            'scope': 'HISTORICAL_FORMATION_ONLY', 'stationarity_assumptions_unverified': True,
            'out_of_sample_required': True, 'live_authority': False}


def current_volatility_regime(series: PriceSeries, *, maxiter: int = 100) -> dict:
    """Fit a two-regime Gaussian model at the current cutoff; filtered endpoint only.

    Historical smoothed probabilities are NOT returned: using them as earlier
    decisions would leak later observations. A whole WhiteBIT regime taxonomy
    or profitable direction signal is not established by this diagnostic.
    """
    from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression
    from threadpoolctl import threadpool_limits
    if type(series) is not PriceSeries or type(maxiter) is not int or not 10 <= maxiter <= 150:
        raise AlphaInputError('PriceSeries and bounded iteration count required')
    _, returns = series.arrays()
    if float(np.std(returns)) < 1e-10:
        return {'status': 'DEGENERATE', 'live_authority': False, 'asof_ms': series.asof_ms}
    with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        result = MarkovRegression(100*returns, k_regimes=2, trend='c',
                                  switching_variance=True).fit(maxiter=maxiter, disp=False)
    if not result.mle_retvals.get('converged', False) or not np.isfinite(result.params).all():
        return {'status': 'FIT_NOT_CONVERGED', 'live_authority': False,
                'asof_ms': series.asof_ms, 'warning_count': len(caught)}
    params = dict(zip(result.model.param_names, result.params))
    variances = np.array([params['sigma2[0]'], params['sigma2[1]']]) / 10000
    probability = np.asarray(result.filtered_marginal_probabilities)[-1]
    if (not np.isfinite(probability).all() or (probability < 0).any() or
            (probability > 1).any() or not np.isclose(probability.sum(), 1) or
            not np.isfinite(variances).all() or (variances <= 0).any()):
        raise AlphaInputError('invalid regime estimate')
    high = int(np.argmax(variances))
    return {'status': 'CURRENT_FILTERED_DIAGNOSTIC', 'asof_ms': series.asof_ms,
            'high_volatility_probability': float(probability[high]),
            'regime_volatility_ratios': np.sqrt(variances).tolist(),
            'method': 'statsmodels.MarkovRegression two-state switching variance',
            'probability_scope': 'CURRENT_ENDPOINT_ONLY_NOT_HISTORICAL_BACKTEST',
            'source_ref': series.source_ref, 'snapshot_id': series.snapshot_id,
            'evidence_class': series.evidence_class, 'warning_count': len(caught),
            'live_authority': False, 'profitability_proven': False}
