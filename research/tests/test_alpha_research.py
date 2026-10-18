"""Real installed CPU libraries and copied upstream source; no backend mocks.

All price fixtures are synthetic. Passing these tests proves code contracts,
not market-data quality, trading profit, WhiteBIT account binding or Windows.
"""
from dataclasses import replace
from decimal import localcontext
from hashlib import sha1
from pathlib import Path
import unittest

import numpy as np

from autotrade_research.integrations.alpha_economics import (
    AlphaInputError, CostBudget, exact, fee_ratios, leverage_scenarios, opportunity)
from autotrade_research.integrations.alpha_models import PriceSeries, forecast_suite, make_split
from autotrade_research.integrations.alpha_packet import build_alpha_packet
from autotrade_research.integrations.alpha_statistics import adjust_hypotheses, cointegration_scan


def series(n=260, market='BTC_USDT'):
    t = np.arange(n)
    prices = 100*np.exp(0.0002*t+0.004*np.sin(t/7))
    return PriceSeries(market, tuple(f'{x:.10f}' for x in prices),
                       tuple(int(i)*60000 for i in t), tuple(int(i)*60000+100 for i in t),
                       60000, (n-1)*60000+100, 'fixture:synthetic-not-market',
                       'fixture-snapshot-1', 'SYNTHETIC_FIXTURE')


def costs(asof=1000):
    return CostBudget('0.001', '0.001', '4', '1', '1', '0', '0.02', '100', '5',
                      'USDT', 'fixture:cost-assumptions', asof-1, asof+1000)


class FeeTests(unittest.TestCase):
    def test_percent_conversion(self):
        f = fee_ratios({'maker':'0.1','taker':'0.1'}, 'BTC_USDT', product='SPOT')
        self.assertEqual(f['taker'], '0.001')
    def test_future_defaults(self):
        f = fee_ratios({'futures_maker':'0.01','futures_taker':'0.035'}, 'BTC_PERP', product='FUTURES')
        self.assertEqual(f['taker'], '0.00035')
    def test_override(self):
        f = fee_ratios({'maker':'0.1','taker':'0.1','custom_fee':{'BTC_USDT':{'maker':'0','taker':'0.05'}}},
                       'BTC_USDT', product='SPOT')
        self.assertEqual(f['taker'], '0.0005')
    def test_bad_override_no_fallback(self):
        self.assertRaises(AlphaInputError, fee_ratios, {'maker':'0.1','taker':'0.1','custom_fee':{'BTC_USDT':None}},
                          'BTC_USDT', product='SPOT')
    def test_withdraw_schema_rejected(self):
        self.assertRaises(AlphaInputError, fee_ratios, {'BTC':{'withdraw':{'fixed':'0.001'}}}, 'BTC_USDT', product='SPOT')
    def test_missing_field(self):
        self.assertRaises(AlphaInputError, fee_ratios, {'maker':'0.1'}, 'BTC_USDT', product='SPOT')
    def test_nonfinite(self):
        self.assertRaises(AlphaInputError, fee_ratios, {'maker':'NaN','taker':'0.1'}, 'BTC_USDT', product='SPOT')
    def test_negative(self):
        self.assertRaises(AlphaInputError, fee_ratios, {'maker':'-1','taker':'0.1'}, 'BTC_USDT', product='SPOT')
    def test_error(self):
        self.assertRaises(AlphaInputError, fee_ratios, {'error':'denied'}, 'BTC_USDT', product='SPOT')
    def test_wrong_product(self):
        self.assertRaises(AlphaInputError, fee_ratios, {}, 'BTC_USDT', product='UNKNOWN')
    def test_float_exact_boundary(self):
        self.assertRaises(AlphaInputError, exact, 0.001, 'rate')
    def test_bool_exact_boundary(self):
        self.assertRaises(AlphaInputError, exact, True, 'rate')


class CostTests(unittest.TestCase):
    def test_full_costs(self):
        o = opportunity('0.01','0.006',costs(),market='BTC_USDT',asof_ms=1000)
        self.assertEqual(o['round_trip_cost_bps'], '28')
        self.assertEqual(o['estimated_quote_gain'], '0.72')
        self.assertTrue(o['candidate']); self.assertFalse(o['live_authority'])
    def test_prediction_below_cost(self):
        o = opportunity('0.001','0',costs(),market='BTC_USDT',asof_ms=1000)
        self.assertEqual(o['reason'], 'NO_EDGE_AFTER_COSTS')
    def test_uncertainty(self):
        o = opportunity('0.01','-0.005',costs(),market='BTC_USDT',asof_ms=1000)
        self.assertEqual(o['reason'], 'UNCERTAINTY_EXCEEDS_EDGE')
    def test_min_notional(self):
        c = replace(costs(), notional_quote='1')
        o = opportunity('0.1','0.05',c,market='BTC_USDT',asof_ms=1000)
        self.assertEqual(o['reason'], 'BELOW_MINIMUM_NOTIONAL')
    def test_stale_cost(self):
        self.assertRaises(AlphaInputError, opportunity, '0.1','0.05',costs(),market='BTC_USDT',asof_ms=3000)
    def test_future_cost(self):
        self.assertRaises(AlphaInputError, opportunity, '0.1','0.05',costs(),market='BTC_USDT',asof_ms=0)
    def test_wrong_quote(self):
        self.assertRaises(AlphaInputError, opportunity, '0.1','0.05',costs(),market='BTC_USD',asof_ms=1000)
    def test_inverse_bounds(self):
        self.assertRaises(AlphaInputError, opportunity, '0.1','0.2',costs(),market='BTC_USDT',asof_ms=1000)
    def test_context_independence(self):
        with localcontext() as ctx:
            ctx.prec=2
            o = opportunity('0.01','0.006',costs(),market='BTC_USDT',asof_ms=1000)
        self.assertEqual(o['estimated_quote_gain'], '0.72')
    def test_leverage_loss_amplified(self):
        r = leverage_scenarios('-0.01',costs(),equity_quote='100',leverage_values=(1,2,3),asof_ms=1000)
        self.assertLess(float(r[2]['estimated_quote_pnl']),float(r[0]['estimated_quote_pnl']))
        self.assertFalse(r[2]['position_enabled'])
        self.assertEqual(r[2]['liquidation_model'],'NOT_MODELED')
    def test_no_duplicate_leverage(self):
        self.assertRaises(AlphaInputError, leverage_scenarios, '0.01',costs(),equity_quote='100',leverage_values=(1,1),asof_ms=1000)
    def test_no_boolean_leverage(self):
        self.assertRaises(AlphaInputError, leverage_scenarios, '0.01',costs(),equity_quote='100',leverage_values=(True,),asof_ms=1000)
    def test_no_zero_notional(self):
        self.assertRaises(AlphaInputError, opportunity, '0.1','0.05',replace(costs(),notional_quote='0'),market='BTC_USDT',asof_ms=1000)


class ChronologyTests(unittest.TestCase):
    def test_purged_horizon(self):
        s = make_split(series(),lookback=24,horizon=6)
        self.assertLess(max(s['label_times'][s['train']]),min(s['decision_times'][s['calibration']]))
        self.assertLess(max(s['label_times'][s['calibration']]),min(s['decision_times'][s['holdout']]))
    def test_future_rejected(self):
        s=series()
        self.assertRaises(AlphaInputError,make_split,replace(s,asof_ms=s.asof_ms-1000),lookback=24,horizon=1)
    def test_gap_rejected(self):
        s=series(); times=list(s.closed_at_ms); times[50]+=1
        self.assertRaises(AlphaInputError,make_split,replace(s,closed_at_ms=tuple(times)),lookback=24,horizon=1)
    def test_float_price_rejected(self):
        s=series(); p=list(s.closes); p[0]=100.0
        self.assertRaises(AlphaInputError,make_split,replace(s,closes=tuple(p)),lookback=24,horizon=1)
    def test_label_availability(self):
        s=series(); a=list(s.available_at_ms); a[-1]=s.asof_ms+1
        self.assertRaises(AlphaInputError,make_split,replace(s,available_at_ms=tuple(a)),lookback=24,horizon=1)
    def test_zero_price_rejected(self):
        s=series(); p=list(s.closes); p[0]='0'
        self.assertRaises(AlphaInputError,make_split,replace(s,closes=tuple(p)),lookback=24,horizon=1)
    def test_stale_price(self):
        s=series()
        self.assertRaises(AlphaInputError,make_split,replace(s,asof_ms=s.asof_ms+600000),lookback=24,horizon=1)
    def test_short_history(self):
        self.assertRaises(AlphaInputError,make_split,series(30),lookback=24,horizon=1)
    def test_lookback_limit(self):
        self.assertRaises(AlphaInputError,make_split,series(),lookback=999,horizon=1)
    def test_horizon_limit(self):
        self.assertRaises(AlphaInputError,make_split,series(),lookback=24,horizon=999)
    def test_series_evidence_required(self):
        self.assertRaises(AlphaInputError,make_split,replace(series(),source_ref=''),lookback=24,horizon=1)
    def test_series_kind_required(self):
        self.assertRaises(AlphaInputError,make_split,replace(series(),evidence_class='REAL_PROFIT'),lookback=24,horizon=1)


class RealModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = forecast_suite(series(),steps=12)
    def test_three_real_libraries(self):
        self.assertEqual(set(self.report['models']), {'DLINEAR','XGBOOST','CATBOOST','FIXED_ENSEMBLE'})
    def test_dlinear_trains_real_parameters(self):
        d = self.report['models']['DLINEAR']
        self.assertTrue(d['weights_changed'])
        self.assertEqual(d['capacity']['parameters'],50)
    def test_no_fake_market_evidence(self):
        self.assertEqual(self.report['evidence_class'],'SYNTHETIC_FIXTURE')
        self.assertFalse(self.report['whitebit_profitability_proven'])
        self.assertFalse(self.report['live_authority'])
    def test_intervals_and_holdout(self):
        for m in self.report['models'].values():
            self.assertLessEqual(float(m['lower_return_ratio']),float(m['gross_midpoint_return_ratio']))
            self.assertLessEqual(float(m['gross_midpoint_return_ratio']),float(m['upper_return_ratio']))
            self.assertTrue(0 <= m['holdout_empirical_interval_coverage'] <= 1)
            self.assertFalse(m['coverage_guaranteed'])
    def test_future_labels_purged(self):
        p=self.report['partitions']
        self.assertLess(p['train_last_label_ms'],p['calibration_first_decision_ms'])
        self.assertLess(p['calibration_last_label_ms'],p['holdout_first_decision_ms'])
    def test_training_not_changed_by_holdout(self):
        s=series(); p=list(s.closes); p[-5]=str(float(p[-5])*1.003)
        r=forecast_suite(replace(s,closes=tuple(p)),steps=12,backends=('XGBOOST','DLINEAR'))
        self.assertEqual(r['models']['XGBOOST']['model_sha256'],self.report['models']['XGBOOST']['model_sha256'])
        self.assertEqual(r['models']['DLINEAR']['weights_after_sha256'],self.report['models']['DLINEAR']['weights_after_sha256'])
    def test_duplicate_model(self):
        self.assertRaises(AlphaInputError,forecast_suite,series(),backends=('DLINEAR','DLINEAR'))
    def test_compute_cap(self):
        self.assertRaises(AlphaInputError,forecast_suite,series(),steps=10000)
    def test_unknown_backend(self):
        self.assertRaises(AlphaInputError,forecast_suite,series(),backends=('DOWNLOAD_UNTRUSTED',))
    def test_vendor_is_exact_source(self):
        path=Path(__file__).parents[1]/'autotrade_research/integrations/vendor/dlinear.py'
        b=path.read_bytes()
        self.assertEqual(sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest(),'1cf739ab3de99497d0225610153a05c52609c760')
    def test_shared_packet_executes_models_and_costs(self):
        s=series(); c=costs(s.asof_ms)
        p=build_alpha_packet(s,c,steps=8,backends=('XGBOOST','DLINEAR'))
        self.assertEqual(p['coordination']['input_snapshot_id'],s.snapshot_id)
        self.assertFalse(p['coordination']['independent_votes'])
        self.assertFalse(p['live_authority'])
        self.assertEqual(len(p['packet_sha256']),64)
        self.assertEqual(len(p['opportunities']),3)


class RealStatisticsTests(unittest.TestCase):
    def test_fdr_correction(self):
        r=adjust_hypotheses((0.001,0.03,0.4))
        self.assertTrue(r['rejected_null'][0])
        self.assertFalse(r['rejected_null'][2])
        self.assertTrue(all(a>=p for a,p in zip(r['adjusted_p_values'],(0.001,0.03,0.4))))
    def test_bad_pvalue(self):
        self.assertRaises(AlphaInputError,adjust_hypotheses,(float('nan'),))
    def test_collinear_not_free_profit(self):
        s=series(); other=replace(s,market='ETH_USDT',snapshot_id='eth')
        r=cointegration_scan((s,other))
        self.assertTrue(r['pairs'][0]['degenerate'])
        self.assertFalse(r['pairs'][0]['hypothesis_flag'])
    def test_real_cointegration_algorithm(self):
        s=series(); rng=np.random.default_rng(11)
        vals=np.exp(np.cumsum(rng.normal(0,.004,len(s.closes))))*100
        p1=tuple(f'{x:.10f}' for x in vals)
        p2=tuple(f'{x:.10f}' for x in (2*vals*np.exp(rng.normal(0,.0005,len(vals)))))
        r=cointegration_scan((replace(s,closes=p1),replace(s,market='ETH_USDT',closes=p2)))
        self.assertTrue(np.isfinite(r['pairs'][0]['p_value']))
        self.assertFalse(r['live_authority'])
    def test_alignment_required(self):
        s=series()
        self.assertRaises(AlphaInputError,cointegration_scan,(s,replace(s,market='ETH_USDT',asof_ms=s.asof_ms+1)))
    def test_duplicate_market(self):
        self.assertRaises(AlphaInputError,cointegration_scan,(series(),series()))




class AvailabilityRegressionTests(unittest.TestCase):
    def test_decision_time_is_ingestion_time(self):
        s = series()
        split = make_split(s, lookback=24, horizon=1)
        self.assertEqual(int(split['decision_times'][0]), s.available_at_ms[24])
        self.assertGreater(int(split['decision_times'][0]), s.closed_at_ms[24])

    def test_elapsed_forecast_rejected(self):
        s = series()
        self.assertRaises(AlphaInputError, make_split,
                          replace(s, asof_ms=s.closed_at_ms[-1]+60000), lookback=24, horizon=1)

    def test_delayed_target_cannot_be_retrospective_decision(self):
        s = series()
        delayed = replace(s, available_at_ms=tuple(v+120000 for v in s.available_at_ms),
                          asof_ms=s.asof_ms+120000)
        self.assertRaises(AlphaInputError, make_split, delayed, lookback=24, horizon=1)


class RegimeTests(unittest.TestCase):
    def test_actual_statsmodels_filter(self):
        from autotrade_research.integrations.alpha_statistics import current_volatility_regime
        rng = np.random.default_rng(17)
        s = series(400)
        ret = np.r_[rng.normal(0, 0.001, 200), rng.normal(0, 0.01, 199)]
        prices = 100*np.exp(np.r_[0, np.cumsum(ret)])
        s = replace(s, closes=tuple(f'{v:.12f}' for v in prices))
        r = current_volatility_regime(s)
        self.assertEqual(r['status'], 'CURRENT_FILTERED_DIAGNOSTIC')
        self.assertTrue(0 <= r['high_volatility_probability'] <= 1)
        self.assertFalse(r['live_authority'])
        self.assertNotIn('smoothed_probabilities', r)

    def test_constant_series_is_not_a_regime_success(self):
        from autotrade_research.integrations.alpha_statistics import current_volatility_regime
        s = series()
        r = current_volatility_regime(replace(s, closes=('100',)*len(s.closes)))
        self.assertEqual(r['status'], 'DEGENERATE')

    def test_regime_budget(self):
        from autotrade_research.integrations.alpha_statistics import current_volatility_regime
        self.assertRaises(AlphaInputError, current_volatility_regime, series(), maxiter=100000)


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import runpy
        cls.ns = runpy.run_path(str(Path(__file__).resolve().parents[2]/'tools/run_whitebit_alpha_research.py'),
                               run_name='offline_alpha_probe')

    def test_actual_packet_from_json_roundtrip(self):
        import json
        payload = json.loads(json.dumps(self.ns['demo_input']()))
        report = self.ns['run_payload'](payload, steps=4)
        self.assertEqual(report['forecast']['evidence_class'], 'SYNTHETIC_FIXTURE')
        self.assertEqual(len(report['opportunities']), 4)

    def test_credentials_not_accepted(self):
        payload = self.ns['demo_input']()
        payload['api_secret'] = 'NOT_A_REAL_SECRET'
        self.assertRaises(AlphaInputError, self.ns['run_payload'], payload)

    def test_duplicate_json_field(self):
        self.assertRaises(AlphaInputError, self.ns['_unique_pairs'], [('series', {}), ('series', {})])

    def test_cli_missing_file_is_nonfinancial_failure(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as buf:
            code = self.ns['main'](['--input', '/definitely-not-an-autotrade-secret-file'])
        self.assertEqual(code, 2)
        self.assertIn('no financial operation', buf.getvalue())


class AttributionTests(unittest.TestCase):
    def test_actual_shap_tree_explainer_additivity(self):
        result = forecast_suite(series(), steps=8, backends=('XGBOOST',))
        m = result['models']['XGBOOST']
        e = m['explanation']
        self.assertEqual(e['method'], 'shap.TreeExplainer')
        self.assertEqual(len(e['features']), 24)
        self.assertEqual(len(e['contributions_return_ratio']), 24)
        self.assertAlmostEqual(e['baseline_return_ratio'] + sum(e['contributions_return_ratio']),
                               e['explained_prediction_ratio'], places=7)
        self.assertAlmostEqual(float(m['gross_midpoint_return_ratio']), e['explained_prediction_ratio'], places=7)
        self.assertFalse(e['causal_explanation'])

if __name__ == '__main__':
    unittest.main()
