"""Digest-bound native alpha checkpoints; inference never fits or unpickles.

The envelope retains the actual causal source and purged split used by the
existing trainer. It is research evidence, not provider or promotion authority.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
from threadpoolctl import threadpool_limits

from .alpha_economics import AlphaInputError

SCHEMA = "autotrade.alpha-model.v1"
MAX_BYTES = 4_000_000
_PACKAGES = {"XGBOOST": "xgboost", "CATBOOST": "catboost", "DLINEAR": "torch"}


def _canonical(document: dict) -> bytes:
    try:
        result = json.dumps(document, sort_keys=True, separators=(",", ":"),
                            allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise AlphaInputError("model checkpoint must be finite canonical JSON") from exc
    if len(result) > MAX_BYTES:
        raise AlphaInputError("model checkpoint exceeds the bounded envelope")
    return result


def _dlinear_source_digest() -> str:
    return sha256((Path(__file__).parent / "vendor" / "dlinear.py").read_bytes()).hexdigest()


def native_payload(backend: str, model, *, normalizers=None) -> dict:
    """Capture an already fitted native model, without a second training path."""
    if backend == "XGBOOST":
        raw = bytes(model.get_booster().save_raw(raw_format="json"))
        return {"format": "XGBOOST_JSON", "data_base64": base64.b64encode(raw).decode("ascii")}
    if backend == "CATBOOST":
        with TemporaryDirectory(prefix="autotrade-alpha-cbm-") as directory:
            path = Path(directory) / "model.cbm"
            model.save_model(str(path), format="cbm")
            raw = path.read_bytes()
        return {"format": "CATBOOST_CBM", "data_base64": base64.b64encode(raw).decode("ascii")}
    if backend == "DLINEAR":
        state = {name: {"dtype": "float32", "shape": list(tensor.shape),
                        "values": tensor.detach().cpu().numpy().tolist()}
                 for name, tensor in model.state_dict().items()}
        return {"format": "DLINEAR_FLOAT32_STATE", "state": state,
                "normalizers": normalizers, "source_sha256": _dlinear_source_digest()}
    raise AlphaInputError("unsupported native model backend")


def make_model_artifact(backend: str, payload: dict, *, series, split: dict,
                        steps: int, seed: int) -> bytes:
    from .alpha_models import _digest
    document = {
        "schema": SCHEMA, "backend": backend,
        "backend_version": version(_PACKAGES[backend]), "numpy_version": version("numpy"),
        "series": asdict(series),
        "training": {"lookback": split["lookback"], "horizon": split["horizon"],
                     "steps": steps, "seed": seed},
        "partitions": {name: split[name].tolist() for name in ("train", "calibration", "holdout")},
        "training_source_sha256": _digest(split["x"][split["train"]],
                                         split["y"][split["train"]]),
        "split_sha256": _digest(split["train"], split["calibration"], split["holdout"],
                                split["decision_times"], split["label_times"]),
        "native": payload, "live_authority": False, "automatic_promotion": False,
    }
    return _canonical(document)


def _validated(data: bytes, expected_sha256: str) -> dict:
    if (type(data) is not bytes or not 1 <= len(data) <= MAX_BYTES or
            type(expected_sha256) is not str or len(expected_sha256) != 64 or
            any(c not in "0123456789abcdef" for c in expected_sha256) or
            sha256(data).hexdigest() != expected_sha256):
        raise AlphaInputError("model checkpoint digest or byte bound differs")
    try:
        document = json.loads(data)
        if type(document) is not dict or _canonical(document) != data:
            raise AlphaInputError("model checkpoint is not canonical JSON")
        if set(document) != {"schema", "backend", "backend_version", "numpy_version",
                             "series", "training", "partitions", "training_source_sha256",
                             "split_sha256", "native", "live_authority", "automatic_promotion"}:
            raise AlphaInputError("unsupported model checkpoint fields")
        backend = document["backend"]
        if (document["schema"] != SCHEMA or type(backend) is not str or backend not in _PACKAGES or
                document["live_authority"] is not False or document["automatic_promotion"] is not False):
            raise AlphaInputError("model checkpoint cannot grant financial authority")
        training = document["training"]
        if (type(training) is not dict or set(training) != {"lookback", "horizon", "steps", "seed"} or
                type(training["steps"]) is not int or not 1 <= training["steps"] <= 128 or
                type(training["seed"]) is not int or not 0 <= training["seed"] <= 2147483647):
            raise AlphaInputError("model checkpoint training bounds differ")
        from .alpha_models import PriceSeries, make_split, _digest
        source = document["series"]
        if type(source) is not dict:
            raise AlphaInputError("model checkpoint causal source is absent")
        source = dict(source)
        for key in ("closes", "closed_at_ms", "available_at_ms"):
            if type(source.get(key)) is not list:
                raise AlphaInputError("model checkpoint source arrays differ")
            source[key] = tuple(source[key])
        series = PriceSeries(**source)
        split = make_split(series, lookback=training["lookback"], horizon=training["horizon"])
        if (document["partitions"] != {name: split[name].tolist() for name in
                                       ("train", "calibration", "holdout")} or
                document["training_source_sha256"] != _digest(split["x"][split["train"]],
                                                             split["y"][split["train"]]) or
                document["split_sha256"] != _digest(split["train"], split["calibration"],
                                                     split["holdout"], split["decision_times"],
                                                     split["label_times"])):
            raise AlphaInputError("model checkpoint causal split differs")
        if (document["numpy_version"] != version("numpy") or
                document["backend_version"] != version(_PACKAGES[backend])):
            raise AlphaInputError("model checkpoint requires the recorded native versions")
        if type(document["native"]) is not dict:
            raise AlphaInputError("native model payload is absent")
        return document
    except AlphaInputError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise AlphaInputError("invalid model checkpoint envelope") from exc


def predict_model_artifact(data: bytes, *, expected_sha256: str, features) -> np.ndarray:
    """Replay a trusted native checkpoint without fit, optimizer or pickle."""
    document = _validated(data, expected_sha256)
    backend, native, training = document["backend"], document["native"], document["training"]
    try:
        values = np.asarray(features, dtype=np.float64)
        if (values.ndim != 2 or not 1 <= len(values) <= 4096 or
                values.shape[1] != training["lookback"] or not np.isfinite(values).all() or
                np.max(np.abs(values)) > 10):
            raise AlphaInputError("bounded model feature matrix required")
        with threadpool_limits(limits=1):
            if backend in ("XGBOOST", "CATBOOST"):
                expected_format = "XGBOOST_JSON" if backend == "XGBOOST" else "CATBOOST_CBM"
                if set(native) != {"format", "data_base64"} or native["format"] != expected_format:
                    raise AlphaInputError("native model format differs")
                raw = base64.b64decode(native["data_base64"], validate=True)
                if not 1 <= len(raw) <= MAX_BYTES:
                    raise AlphaInputError("native model bytes exceed the bounded contract")
                if backend == "XGBOOST":
                    from xgboost import Booster, DMatrix
                    model = Booster()
                    model.load_model(bytearray(raw))
                    model.set_param({"nthread": 1})
                    if model.num_features() != training["lookback"] or model.num_boosted_rounds() != training["steps"]:
                        raise AlphaInputError("native XGBoost dimensions differ")
                    predictions = model.predict(DMatrix(values))
                else:
                    from catboost import CatBoostRegressor
                    with TemporaryDirectory(prefix="autotrade-alpha-replay-") as directory:
                        path = Path(directory) / "model.cbm"
                        path.write_bytes(raw)
                        model = CatBoostRegressor()
                        model.load_model(str(path), format="cbm")
                    if model.tree_count_ != training["steps"] or len(model.feature_names_) != training["lookback"]:
                        raise AlphaInputError("native CatBoost dimensions differ")
                    predictions = model.predict(values, thread_count=1)
            else:
                predictions = _predict_dlinear(native, values, training["lookback"])
        predictions = np.asarray(predictions, dtype=np.float64)
        if predictions.shape != (len(values),) or not np.isfinite(predictions).all():
            raise AlphaInputError("native checkpoint returned invalid predictions")
        return predictions
    except AlphaInputError:
        raise
    except Exception as exc:
        raise AlphaInputError("native model checkpoint could not be replayed") from exc


def _predict_dlinear(native: dict, features: np.ndarray, lookback: int) -> np.ndarray:
    import torch
    from .vendor.dlinear import Model
    if (set(native) != {"format", "state", "normalizers", "source_sha256"} or
            native["format"] != "DLINEAR_FLOAT32_STATE" or
            native["source_sha256"] != _dlinear_source_digest()):
        raise AlphaInputError("DLinear implementation identity differs")
    normalizers = native["normalizers"]
    if (type(normalizers) is not dict or set(normalizers) != {"scale", "yscale", "ymean"} or
            any(type(v) not in (float, int) or not np.isfinite(v) for v in normalizers.values()) or
            normalizers["scale"] < 1e-6 or normalizers["yscale"] < 1e-6):
        raise AlphaInputError("DLinear train-only normalizers differ")
    old_threads = torch.get_num_threads()
    try:
        torch.set_num_threads(1)
        with torch.random.fork_rng(devices=[]):
            model = Model(SimpleNamespace(seq_len=lookback, pred_len=1, enc_in=1, individual=False))
            expected = model.state_dict()
            state = native["state"]
            if type(state) is not dict or set(state) != set(expected):
                raise AlphaInputError("DLinear tensor key set differs")
            loaded = {}
            for name, target in expected.items():
                record = state[name]
                if (type(record) is not dict or set(record) != {"dtype", "shape", "values"} or
                        record["dtype"] != "float32" or record["shape"] != list(target.shape)):
                    raise AlphaInputError("DLinear tensor metadata differs")
                tensor = torch.tensor(record["values"], dtype=torch.float32)
                if tuple(tensor.shape) != tuple(target.shape) or not torch.isfinite(tensor).all():
                    raise AlphaInputError("DLinear tensor values differ")
                loaded[name] = tensor
            model.load_state_dict(loaded, strict=True)
            model.eval()
            with torch.no_grad():
                prediction = model(torch.tensor(features / normalizers["scale"],
                                                dtype=torch.float32).unsqueeze(-1))
            return prediction.numpy().reshape(-1) * normalizers["yscale"] + normalizers["ymean"]
    finally:
        torch.set_num_threads(old_threads)
