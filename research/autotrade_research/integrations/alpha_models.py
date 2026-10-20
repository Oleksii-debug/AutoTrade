"""Bounded, local-only quantitative specialists over one causal WhiteBIT series.

Uses real DLinear, XGBoost and CatBoost implementations. Models are fitted in an
isolated research process, never granted order/credential/promotion authority.
No pretrained weights or models are fetched. Holdout is evaluated, not selected on.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import ceil
from threading import Lock
from types import SimpleNamespace
import json

import numpy as np
from threadpoolctl import threadpool_limits

from .alpha_economics import AlphaInputError, exact, market_name, timestamp

_TRAINING_LOCK = Lock()
BACKENDS = ('DLINEAR', 'XGBOOST', 'CATBOOST')


@dataclass(frozen=True)
class PriceSeries:
    market: str
    closes: tuple[str, ...]
    closed_at_ms: tuple[int, ...]
    available_at_ms: tuple[int, ...]
    interval_ms: int
    asof_ms: int
    source_ref: str
    snapshot_id: str
    evidence_class: str

    def arrays(self):
        market_name(self.market)
        timestamp(self.asof_ms, 'asof')
        if type(self.interval_ms) is not int or not 1 <= self.interval_ms <= 86400000:
            raise AlphaInputError('interval must be 1 ms .. 1 day')
        if any(type(v) is not tuple for v in (self.closes, self.closed_at_ms, self.available_at_ms)):
            raise AlphaInputError('series fields must be tuples')
        n = len(self.closes)
        if not 160 <= n <= 4096 or len(self.closed_at_ms) != n or len(self.available_at_ms) != n:
            raise AlphaInputError('aligned series requires 160..4096 closed bars')
        if self.evidence_class not in ('RECORDED_PUBLIC', 'SYNTHETIC_FIXTURE'):
            raise AlphaInputError('explicit evidence class required')
        for name in ('source_ref', 'snapshot_id'):
            if type(getattr(self, name)) is not str or not 1 <= len(getattr(self, name)) <= 512:
                raise AlphaInputError('snapshot and evidence reference required')
        for i, (closed, available) in enumerate(zip(self.closed_at_ms, self.available_at_ms)):
            timestamp(closed, 'bar close'); timestamp(available, 'bar availability')
            if not closed <= available <= self.asof_ms:
                raise AlphaInputError('future bar, unclosed bar or invalid availability')
            if i and closed-self.closed_at_ms[i-1] != self.interval_ms:
                raise AlphaInputError('duplicate, irregular or missing bars')
            if i and available < self.available_at_ms[i-1]:
                raise AlphaInputError('availability must be nondecreasing')
        if self.asof_ms-self.closed_at_ms[-1] > 2*self.interval_ms:
            raise AlphaInputError('latest price series is stale at decision cutoff')
        values = np.array([float(exact(v, 'close', low='0.000000000001', high='1000000000000'))
                           for v in self.closes], dtype=np.float64)
        ratios = values[1:]/values[:-1]-1
        if not np.isfinite(ratios).all() or np.max(np.abs(ratios)) > 10:
            raise AlphaInputError('implausible/nonfinite price return')
        return values, ratios


def make_split(series: PriceSeries, *, lookback: int, horizon: int) -> dict:
    if type(series) is not PriceSeries:
        raise AlphaInputError('PriceSeries required')
    if type(lookback) is not int or not 4 <= lookback <= 96:
        raise AlphaInputError('lookback must be 4..96')
    if type(horizon) is not int or not 1 <= horizon <= 24:
        raise AlphaInputError('horizon must be 1..24')
    p, r = series.arrays()
    if series.closed_at_ms[-1] + horizon*series.interval_ms <= series.asof_ms:
        raise AlphaInputError('forecast horizon already elapsed at decision cutoff')
    t = np.arange(lookback, len(p)-horizon)
    # A feature can be used only after ingestion, not at its exchange close time.
    # Discard retrospective decisions whose entire target interval already ended.
    t = np.array([i for i in t if series.closed_at_ms[i+horizon] > series.available_at_ms[i]],
                 dtype=np.int64)
    x = np.array([r[i-lookback:i] for i in t])
    y = np.array([p[i+horizon]/p[i]-1 for i in t])
    decision_times = np.asarray(series.available_at_ms, dtype=np.int64)[t]
    label_times = np.asarray(series.available_at_ms, dtype=np.int64)[t+horizon]
    a, b = int(len(t)*0.6), int(len(t)*0.8)
    if not 0 < a < b < len(t):
        raise AlphaInputError('insufficient chronological partitions')
    train = np.flatnonzero((np.arange(len(t)) < a) & (label_times < decision_times[a]))
    calibration = np.flatnonzero((np.arange(len(t)) >= a) & (np.arange(len(t)) < b) &
                                 (label_times < decision_times[b]))
    holdout = np.arange(b, len(t))
    if min(len(train), len(calibration), len(holdout)) < 20:
        raise AlphaInputError('not enough mature, purged labels per partition')
    return dict(x=x, y=y, train=train, calibration=calibration, holdout=holdout,
                current=r[-lookback:][None, :], decision_times=decision_times,
                label_times=label_times, lookback=lookback, horizon=horizon)


def _digest(*arrays) -> str:
    h = sha256()
    for a in arrays:
        a = np.asarray(a)
        h.update(str(a.shape).encode()); h.update(str(a.dtype).encode())
        h.update(a.tobytes(order='C'))
    return h.hexdigest()


def _predict_backend(name: str, split: dict, *, steps: int, seed: int, artifact_sink=None):
    tr = split['train']
    x, y = split['x'][tr], split['y'][tr]
    inference = np.vstack((split['x'], split['current']))
    with threadpool_limits(limits=1):
        if name == 'XGBOOST':
            from xgboost import XGBRegressor
            model = XGBRegressor(n_estimators=steps, max_depth=3, learning_rate=0.05,
                                 objective='reg:squarederror', tree_method='hist',
                                 n_jobs=1, random_state=seed)
            model.fit(x, y)
            import shap
            explanation = shap.TreeExplainer(model)(split['current'])
            contributions = np.asarray(explanation.values)[0]
            base = float(np.asarray(explanation.base_values).reshape(-1)[0])
            current_prediction = float(model.predict(split['current'])[0])
            if (not np.isfinite(contributions).all() or not np.isfinite(base) or
                    not np.isclose(base + float(contributions.sum()), current_prediction, atol=1e-7, rtol=1e-5)):
                raise AlphaInputError('SHAP additivity check failed')
            if artifact_sink is not None:
                from .alpha_model_checkpoint import native_payload
                artifact_sink[name] = native_payload(name, model)
            return np.asarray(model.predict(inference), dtype=float), {
                'fit_implementation': 'xgboost.XGBRegressor',
                'model_sha256': sha256(bytes(model.get_booster().save_raw(raw_format='json'))).hexdigest(),
                'capacity': {'trees': steps, 'max_depth': 3},
                'explanation': {'method': 'shap.TreeExplainer', 'baseline_return_ratio': base,
                                'features': [f'return_lag_{i}' for i in range(split['lookback'], 0, -1)],
                                'contributions_return_ratio': contributions.tolist(),
                                'explained_prediction_ratio': current_prediction,
                                'causal_explanation': False}}
        if name == 'CATBOOST':
            from catboost import CatBoostRegressor
            model = CatBoostRegressor(iterations=steps, depth=3, learning_rate=0.05,
                                      loss_function='RMSE', thread_count=1,
                                      random_seed=seed, verbose=False, allow_writing_files=False)
            model.fit(x, y)
            pred = np.asarray(model.predict(inference), dtype=float)
            if artifact_sink is not None:
                from .alpha_model_checkpoint import native_payload
                artifact_sink[name] = native_payload(name, model)
            # Numeric signature only, not serialization or proof of model file persistence.
            return pred, {'fit_implementation': 'catboost.CatBoostRegressor',
                          'prediction_sha256': _digest(pred),
                          'capacity': {'trees': steps, 'depth': 3}}
        if name != 'DLINEAR':
            raise AlphaInputError('unsupported predictor')
        import torch
        from .vendor.dlinear import Model
        # Scoped RNG and thread setting: intended for an isolated research worker.
        old_threads = torch.get_num_threads()
        try:
            torch.set_num_threads(1)
            with torch.random.fork_rng(devices=[]):
                torch.manual_seed(seed)
                model = Model(SimpleNamespace(seq_len=split['lookback'], pred_len=1,
                                              enc_in=1, individual=False))
                # Both normalizers are fitted on TRAIN ONLY, never on cal/holdout.
                scale = max(float(np.std(x)), 1e-6)
                yscale = max(float(np.std(y)), 1e-6)
                ymean = float(np.mean(y))
                xt = torch.tensor(x/scale, dtype=torch.float32).unsqueeze(-1)
                yt = torch.tensor((y-ymean)/yscale, dtype=torch.float32).reshape(-1, 1, 1)
                opt = torch.optim.Adam(model.parameters(), lr=0.01)
                before = _digest(*(v.detach().numpy() for v in model.state_dict().values()))
                model.train()
                for _ in range(steps):
                    opt.zero_grad(set_to_none=True)
                    loss = torch.mean((model(xt)-yt)**2)
                    if not torch.isfinite(loss):
                        raise AlphaInputError('nonfinite training loss')
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    opt.step()
                model.eval()
                with torch.no_grad():
                    pred = model(torch.tensor(inference/scale, dtype=torch.float32).unsqueeze(-1))
                after = _digest(*(v.detach().numpy() for v in model.state_dict().values()))
                if artifact_sink is not None:
                    from .alpha_model_checkpoint import native_payload
                    artifact_sink[name] = native_payload(name, model, normalizers={
                        'scale': scale, 'yscale': yscale, 'ymean': ymean})
                return pred.numpy().reshape(-1)*yscale+ymean, {
                    'fit_implementation': 'cure-lab/LTSF-Linear DLinear',
                    'weights_before_sha256': before, 'weights_after_sha256': after,
                    'weights_changed': before != after,
                    'capacity': {'parameters': sum(p.numel() for p in model.parameters()), 'epochs': steps},
                    'pretrained': False}
        finally:
            torch.set_num_threads(old_threads)


def forecast_suite(series: PriceSeries, *, lookback: int = 24, horizon: int = 1,
                   steps: int = 32, seed: int = 7,
                   backends: tuple[str, ...] = BACKENDS, alpha: float = 0.1,
                   model_artifacts: dict[str, bytes] | None = None) -> dict:
    """Fit three explicit candidates and a fixed equal-weight ensemble.

    Fit on early 60%; calibrate absolute residual radius on next 20%; report
    untouched later 20%. Purge crossing outcome intervals at both boundaries.
    Latest forecast still uses that frozen model, never a silent holdout refit.
    Temporal dependence may invalidate nominal conformal coverage.
    """
    if model_artifacts is not None and (type(model_artifacts) is not dict or model_artifacts):
        raise AlphaInputError('model_artifacts must be an empty exact dictionary')
    if type(steps) is not int or not 1 <= steps <= 128:
        raise AlphaInputError('steps must be 1..128')
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise AlphaInputError('invalid seed')
    if type(alpha) not in (float, int) or not 0.05 <= alpha <= 0.4:
        raise AlphaInputError('alpha must be 0.05..0.4')
    if (type(backends) is not tuple or not 1 <= len(backends) <= 3 or
            any(type(n) is not str or n not in BACKENDS for n in backends) or
            len(set(backends)) != len(backends)):
        raise AlphaInputError('select distinct supported predictors')
    split = make_split(series, lookback=lookback, horizon=horizon)
    tr, cal, ho = (split[k] for k in ('train', 'calibration', 'holdout'))
    predictions, metadata, native = {}, {}, {}
    with _TRAINING_LOCK:
        for name in backends:
            prediction, meta = _predict_backend(name, split, steps=steps, seed=seed,
                                               artifact_sink=native if model_artifacts is not None else None)
            if prediction.shape != (len(split['y'])+1,) or not np.isfinite(prediction).all():
                raise AlphaInputError('invalid backend forecast')
            predictions[name], metadata[name] = prediction, meta
    if len(backends) > 1:
        predictions['FIXED_ENSEMBLE'] = np.mean(np.stack(list(predictions.values())), axis=0)
        metadata['FIXED_ENSEMBLE'] = {'members': list(backends), 'weights': 'EQUAL_FIXED_BEFORE_HOLDOUT'}
    reports = {}
    for name, pred in predictions.items():
        residuals = np.abs(split['y'][cal]-pred[cal])
        k = ceil((len(cal)+1)*(1-alpha))
        if k > len(cal):
            raise AlphaInputError('too few calibration observations')
        radius = float(np.partition(residuals, k-1)[k-1])
        held = split['y'][ho]
        forecast = float(pred[-1])
        reports[name] = {
            **metadata[name], 'gross_midpoint_return_ratio': format(forecast, '.12f'),
            'lower_return_ratio': format(forecast-radius, '.12f'), 'upper_return_ratio': format(forecast+radius, '.12f'),
            'empirical_calibration_radius': str(radius),
            'holdout_mae': float(np.mean(np.abs(held-pred[ho]))),
            'holdout_zero_return_mae': float(np.mean(np.abs(held))),
            'holdout_empirical_interval_coverage': float(np.mean(np.abs(held-pred[ho]) <= radius)),
            'calibration_nominal_coverage': 1-alpha,
            'coverage_guaranteed': False, 'holdout_used_for_model_selection': False}
    payload_hash = sha256(json.dumps({'prices': series.closes, 'times': series.closed_at_ms,
                                     'availability': series.available_at_ms}, separators=(',', ':')).encode()).hexdigest()
    if model_artifacts is not None:
        from .alpha_model_checkpoint import make_model_artifact
        completed = {name: make_model_artifact(name, native[name], series=series,
                     split=split, steps=steps, seed=seed) for name in backends}
        for name, data in completed.items():
            reports[name]['model_artifact_sha256'] = sha256(data).hexdigest()
        model_artifacts.update(completed)
    return {'schema_version': '1.0', 'provider': 'WHITEBIT', 'market': series.market,
            'snapshot_id': series.snapshot_id, 'source_ref': series.source_ref,
            'evidence_class': series.evidence_class, 'input_sha256': payload_hash,
            'asof_ms': series.asof_ms, 'horizon_bars': horizon, 'interval_ms': series.interval_ms,
            'reference_close_at_ms': series.closed_at_ms[-1],
            'forecast_target_close_at_ms': series.closed_at_ms[-1] + horizon*series.interval_ms,
            'return_reference': 'LAST_OBSERVED_CLOSE_NOT_EXECUTABLE_PRICE',
            'partitions': {'train': len(tr), 'calibration': len(cal), 'holdout': len(ho),
                           'train_last_label_ms': int(split['label_times'][tr[-1]]),
                           'calibration_first_decision_ms': int(split['decision_times'][cal[0]]),
                           'calibration_last_label_ms': int(split['label_times'][cal[-1]]),
                           'holdout_first_decision_ms': int(split['decision_times'][ho[0]])},
            'models': reports, 'mode': 'SHADOW_RESEARCH_ONLY', 'live_authority': False,
            'automatic_promotion': False, 'whitebit_profitability_proven': False,
            'integration_state': 'CALLABLE_RESEARCH_COMPONENT_NOT_HOST_BOUND'}


def predict_model_artifact(data: bytes, *, expected_sha256: str, features):
    from .alpha_model_checkpoint import predict_model_artifact as predict
    return predict(data, expected_sha256=expected_sha256, features=features)
