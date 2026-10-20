"""Native checkpoint replay uses fitted bytes; backend absence is an explicit skip."""
from dataclasses import replace
from hashlib import sha256
from importlib.util import find_spec
import json
import unittest
from unittest.mock import patch

_MISSING_CORE = [name for name in ("numpy", "threadpoolctl") if find_spec(name) is None]
if _MISSING_CORE:
    raise unittest.SkipTest("optional alpha checkpoint dependencies missing: " + ", ".join(_MISSING_CORE))

import numpy as np

from autotrade_research.integrations.alpha_economics import AlphaInputError
from autotrade_research.integrations.alpha_models import (
    PriceSeries, forecast_suite, make_split, predict_model_artifact,
)


def series(n=260):
    indices = np.arange(n)
    values = 100 * np.exp(0.0002 * indices + 0.004 * np.sin(indices / 7))
    return PriceSeries("BTC_USDT", tuple(format(v, ".10f") for v in values),
        tuple(int(i) * 60_000 for i in indices),
        tuple(int(i) * 60_000 + 100 for i in indices), 60_000,
        (n - 1) * 60_000 + 100, "fixture:synthetic", "native-checkpoint-fixture",
        "SYNTHETIC_FIXTURE")


def encoded(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class AlphaCheckpointGuardTests(unittest.TestCase):
    def test_digest_precedes_native_loading(self):
        with self.assertRaisesRegex(AlphaInputError, "digest"):
            predict_model_artifact(b"{}", expected_sha256="0" * 64, features=[[0] * 24])

    def test_noncanonical_json_is_rejected_before_backend_loading(self):
        for data in (b'{ "schema": "x" }', b'{"schema":"x","schema":"y"}', b'{"x":NaN}'):
            with self.subTest(data=data), self.assertRaises(AlphaInputError):
                predict_model_artifact(data, expected_sha256=sha256(data).hexdigest(), features=[[0] * 24])

    def test_collector_requires_empty_exact_dict(self):
        class Subdict(dict):
            pass
        for collector in ({"prior": b"keep"}, [], Subdict()):
            with self.subTest(collector=collector), self.assertRaises(AlphaInputError):
                forecast_suite(series(), steps=2, model_artifacts=collector)

    def test_partial_fit_never_publishes_a_partial_collector(self):
        artifacts = {}
        def backend(name, split, *, steps, seed, artifact_sink):
            if name == "CATBOOST":
                raise AlphaInputError("second backend failed")
            artifact_sink[name] = {"first": "already fitted"}
            return np.zeros(len(split["y"]) + 1), {}
        with patch("autotrade_research.integrations.alpha_models._predict_backend", backend):
            with self.assertRaisesRegex(AlphaInputError, "second backend"):
                forecast_suite(series(), steps=2, backends=("XGBOOST", "CATBOOST"), model_artifacts=artifacts)
        self.assertEqual(artifacts, {})


_REQUIRED = ("torch", "xgboost", "catboost", "shap")
_MISSING = [name for name in _REQUIRED if find_spec(name) is None]


@unittest.skipIf(bool(_MISSING), "real native alpha backends missing: " + ", ".join(_MISSING))
class NativeAlphaCheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = series()
        cls.artifacts = {}
        cls.report = forecast_suite(cls.source, steps=4, model_artifacts=cls.artifacts)
        cls.split = make_split(cls.source, lookback=24, horizon=1)
        cls.features = np.vstack((cls.split["x"], cls.split["current"]))

    def replay(self, backend, features=None, data=None):
        data = self.artifacts[backend] if data is None else data
        return predict_model_artifact(data, expected_sha256=sha256(data).hexdigest(),
                                      features=self.features if features is None else features)

    def test_real_native_models_reproduce_forecast_and_held_out_error(self):
        self.assertEqual(set(self.artifacts), {"DLINEAR", "XGBOOST", "CATBOOST"})
        for backend, data in self.artifacts.items():
            with self.subTest(backend=backend):
                report = self.report["models"][backend]
                predictions = self.replay(backend)
                self.assertEqual(report["model_artifact_sha256"], sha256(data).hexdigest())
                self.assertAlmostEqual(float(report["gross_midpoint_return_ratio"]), float(predictions[-1]), places=10)
                held = self.split["holdout"]
                error = float(np.mean(np.abs(self.split["y"][held] - predictions[held])))
                self.assertAlmostEqual(report["holdout_mae"], error, places=10)

    def test_restoration_does_not_fit_or_construct_an_optimizer(self):
        forbidden = AssertionError("checkpoint replay attempted training")
        with patch("xgboost.XGBRegressor.fit", side_effect=forbidden), \
             patch("catboost.CatBoostRegressor.fit", side_effect=forbidden), \
             patch("torch.optim.Adam", side_effect=forbidden):
            for backend in self.artifacts:
                with self.subTest(backend=backend):
                    self.assertEqual(self.replay(backend).shape, (len(self.features),))

    def test_native_formats_and_full_causal_provenance_are_retained(self):
        expected = {"XGBOOST": "XGBOOST_JSON", "CATBOOST": "CATBOOST_CBM", "DLINEAR": "DLINEAR_FLOAT32_STATE"}
        for backend, data in self.artifacts.items():
            with self.subTest(backend=backend):
                envelope = json.loads(data)
                self.assertEqual(envelope["native"]["format"], expected[backend])
                self.assertEqual(envelope["series"]["snapshot_id"], self.source.snapshot_id)
                self.assertEqual(envelope["series"]["available_at_ms"], list(self.source.available_at_ms))
                for partition in ("train", "calibration", "holdout"):
                    self.assertEqual(envelope["partitions"][partition], self.split[partition].tolist())
                self.assertFalse(envelope["live_authority"])
                self.assertFalse(envelope["automatic_promotion"])

    def test_modified_causal_source_or_partition_is_rejected(self):
        for change in ("source", "partition"):
            envelope = json.loads(self.artifacts["DLINEAR"])
            if change == "source":
                envelope["series"]["closes"][10] = "101"
            else:
                envelope["partitions"]["train"].append(envelope["partitions"]["holdout"][0])
            with self.subTest(change=change), self.assertRaises(AlphaInputError):
                self.replay("DLINEAR", data=encoded(envelope))

    def test_modified_version_or_authority_is_rejected(self):
        for key, value in (("backend_version", "0.0.invalid"), ("live_authority", True), ("automatic_promotion", True)):
            envelope = json.loads(self.artifacts["XGBOOST"])
            envelope[key] = value
            with self.subTest(key=key), self.assertRaises(AlphaInputError):
                self.replay("XGBOOST", data=encoded(envelope))

    def test_dlinear_normalizers_and_tensor_shape_are_checked(self):
        for change in ("scale", "shape"):
            envelope = json.loads(self.artifacts["DLINEAR"])
            if change == "scale":
                envelope["native"]["normalizers"]["scale"] = 0
            else:
                first = next(iter(envelope["native"]["state"].values()))
                first["shape"] = [999]
            with self.subTest(change=change), self.assertRaises(AlphaInputError):
                self.replay("DLINEAR", data=encoded(envelope))

    def test_native_dimensions_and_nonfinite_features_are_rejected(self):
        for features in (np.zeros((2, 23)), np.full((2, 24), np.nan), np.zeros((0, 24))):
            with self.subTest(shape=features.shape), self.assertRaises(AlphaInputError):
                self.replay("XGBOOST", features=features)

    def test_default_report_does_not_claim_model_files(self):
        report = forecast_suite(self.source, steps=2, backends=("XGBOOST",))
        self.assertNotIn("model_artifact_sha256", report["models"]["XGBOOST"])


if __name__ == "__main__":
    unittest.main()
