"""Read-only, research-only adapters for optional upstream open-source packages.

No API keys, no broker/order authority, no model promotion. This code may be run
only in an isolated research worker. The financial host MUST NOT import it.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any

PUBLIC_CCXT_VENUES = frozenset({
    "binance", "bybit", "kraken", "coinbase", "okx", "whitebit",
    "bitstamp", "bitfinex", "deribit",
})
TIMEFRAMES = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})


def _positive_int(value: Any, name: str, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise ValueError(f"{name} must be an integer in 1..{maximum}")
    return value


def _nonnegative_ms(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative epoch millisecond integer")
    return value


def _number(value: Any, name: str, *, minimum: Decimal | None = None,
            maximum: Decimal | None = None) -> Decimal:
    # Quantitative research signals may be converted to float; cash never is.
    if type(value) is bool or isinstance(value, float):
        raise ValueError(f"{name} must be a finite decimal string or integer")
    try:
        result = Decimal(value)
    except (TypeError, InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite() or len(result.as_tuple().digits) > 48:
        raise ValueError(f"{name} is invalid or too large")
    if (minimum is not None and result < minimum) or (maximum is not None and result > maximum):
        raise ValueError(f"{name} outside allowed range")
    return result


def _market_number(value: Any, name: str) -> Decimal:
    # Exchange clients commonly deserialize OHLCV JSON numerics as floats.
    # Exact-value cash authority remains outside this research data adapter.
    if type(value) is float:
        if not (-float("inf") < value < float("inf")):
            raise ValueError(f"{name} must be finite")
        return _number(str(value), name, minimum=Decimal("0"))
    return _number(value, name, minimum=Decimal("0"))


def _feature_map(raw: Any) -> dict[str, float]:
    if type(raw) is not dict or not 1 <= len(raw) <= 16:
        raise ValueError("features must contain 1..16 exact string keys")
    out = {}
    for key, value in raw.items():
        if type(key) is not str or not key or len(key) > 64 or not key.replace("_", "").isalnum():
            raise ValueError("invalid feature name")
        out[key] = float(_number(value, key, minimum=Decimal("-1000000"), maximum=Decimal("1000000")))
    return out


def public_ohlcv_ccxt(exchange_id: str, symbol: str, timeframe: str = "1h",
                      limit: int = 100) -> tuple[dict[str, str | int], ...]:
    """Read public CCXT market bars and return only inert data.

    Never accepts credentials, exposes the client or calls trading methods.
    """
    if type(exchange_id) is not str or exchange_id not in PUBLIC_CCXT_VENUES:
        raise ValueError("exchange not approved for public research")
    if type(symbol) is not str or not 3 <= len(symbol) <= 40 or not all(
        c.isupper() or c.isdigit() or c in "/:-" for c in symbol
    ) or "/" not in symbol:
        raise ValueError("invalid canonical CCXT market symbol")
    if timeframe not in TIMEFRAMES:
        raise ValueError("unsupported timeframe")
    limit = _positive_int(limit, "limit", 500)
    lib = import_module("ccxt")
    client_type = getattr(lib, exchange_id)
    client = client_type({"enableRateLimit": True, "timeout": 10000})
    try:
        raw = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if type(raw) not in (list, tuple) or len(raw) > limit:
            raise ValueError("invalid public OHLCV response length")
        result = []
        last_ms = -1
        for item in raw:
            if type(item) not in (list, tuple) or len(item) != 6:
                raise ValueError("invalid OHLCV row")
            when = _nonnegative_ms(item[0], "bar timestamp")
            if when <= last_ms:
                raise ValueError("duplicate/out-of-order OHLCV rows")
            last_ms = when
            op, high, low, close, volume = (
                _market_number(x, field)
                for x, field in zip(item[1:], ("open", "high", "low", "close", "volume"))
            )
            if low > min(op, close) or high < max(op, close) or low > high:
                raise ValueError("incoherent OHLCV high/low")
            result.append({"timestamp_ms": when, "open": str(op), "high": str(high),
                           "low": str(low), "close": str(close), "volume": str(volume)})
        return tuple(result)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def river_shadow_probability(training: list[dict[str, Any]], current_features: dict[str, Any],
                             decision_cutoff_ms: int) -> dict[str, Any]:
    """Train a transient River estimator only on mature outcomes; no self-promotion."""
    cutoff = _nonnegative_ms(decision_cutoff_ms, "decision_cutoff_ms")
    if type(training) is not list or not 1 <= len(training) <= 5000:
        raise ValueError("training requires 1..5000 completed outcomes")
    xnow = _feature_map(current_features)
    rows = []
    last_seen = -1
    for item in training:
        if type(item) is not dict:
            raise ValueError("training rows must be dictionaries")
        observed = _nonnegative_ms(item.get("observed_at_ms"), "observed_at_ms")
        mature = _nonnegative_ms(item.get("label_available_at_ms"), "label_available_at_ms")
        if mature < observed or mature > cutoff:
            raise ValueError("future, early or unsettled training outcome")
        if observed < last_seen:
            raise ValueError("training rows are not chronologically ordered")
        last_seen = observed
        label = item.get("label")
        if type(label) is not int or label not in (0, 1):
            raise ValueError("label must be exactly 0 or 1")
        features = _feature_map(item.get("features"))
        if features.keys() != xnow.keys():
            raise ValueError("training/current feature schema mismatch")
        rows.append((features, bool(label)))
    river = import_module("river")
    model = river.linear_model.LogisticRegression()
    for features, label in rows:
        model.learn_one(features, label)
    probability = model.predict_proba_one(xnow).get(True, 0.0)
    if type(probability) not in (int, float) or not 0 <= probability <= 1:
        raise ValueError("invalid River prediction")
    return {"mode": "SHADOW_RESEARCH_ONLY", "labels_used": len(rows),
            "decision_cutoff_ms": cutoff, "probability_up": str(probability),
            "trading_authority": "NONE", "promotion_authority": "NONE"}


def optuna_shadow_threshold(cases: list[dict[str, Any]], decision_cutoff_ms: int,
                            trials: int = 24, seed: int = 7) -> dict[str, Any]:
    """Search a research-only threshold on development data (never holdout)."""
    cutoff = _nonnegative_ms(decision_cutoff_ms, "decision_cutoff_ms")
    trials = _positive_int(trials, "trials", 100)
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("seed outside allowed integer range")
    if type(cases) is not list or not 2 <= len(cases) <= 5000:
        raise ValueError("need 2..5000 development observations")
    rows = []
    for item in cases:
        if type(item) is not dict:
            raise ValueError("development rows must be dictionaries")
        observed = _nonnegative_ms(item.get("observed_at_ms"), "observed_at_ms")
        mature = _nonnegative_ms(item.get("label_available_at_ms"), "label_available_at_ms")
        if mature < observed or mature > cutoff:
            raise ValueError("future or unsettled development label")
        label = item.get("label")
        if type(label) is not int or label not in (0, 1):
            raise ValueError("invalid binary label")
        signal = _number(item.get("signal"), "signal", minimum=Decimal("0"), maximum=Decimal("1"))
        rows.append((signal, label))
    optuna = import_module("optuna")
    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)

    def score(trial):
        threshold = Decimal(str(trial.suggest_float("threshold", 0.0, 1.0)))
        return sum(int((signal >= threshold) == bool(label)) for signal, label in rows) / len(rows)

    study.optimize(score, n_trials=trials, n_jobs=1, show_progress_bar=False)
    return {"mode": "DEVELOPMENT_SEARCH_ONLY", "development_rows": len(rows),
            "trial_count": trials, "best_threshold": str(study.best_params["threshold"]),
            "in_sample_accuracy": str(study.best_value),
            "holdout_validated": False, "trading_authority": "NONE",
            "promotion_authority": "NONE"}
