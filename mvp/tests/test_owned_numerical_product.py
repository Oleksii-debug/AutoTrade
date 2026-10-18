"""Product lifecycle wiring without external network or model calls."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.product_runtime import build_product
from mvp.autotrade_mvp.owned_numerical_research import OwnedResearchError


class OwnedNumericalProductTests(unittest.TestCase):
    def test_windows_launcher_forwards_only_explicit_public_learning_opt_in(self):
        source = (Path(__file__).resolve().parents[2] / 'src' / 'AutoTrade.Desktop'
                  / 'OwnedProviderFreeRuntime.cs').read_text(encoding='utf-8')
        self.assertIn('bool publicLearning = false', source)
        self.assertIn('Array.IndexOf(Environment.GetCommandLineArgs(), "--public-learning") >= 0', source)
        self.assertIn('if (publicLearning)\n            start.ArgumentList.Add("--public-learning");', source)

    def test_default_remains_offline(self):
        with TemporaryDirectory() as root, patch(
            'mvp.autotrade_mvp.owned_numerical_research.OwnedNumericalResearch'
        ) as owner:
            runtime, _ = build_product(root, desktop_session_sink=lambda *_: None)
            runtime.close()
            owner.assert_not_called()

    def test_explicit_start_and_drain_before_host_fence_release(self):
        with TemporaryDirectory() as root, patch(
            'mvp.autotrade_mvp.owned_numerical_research.OwnedNumericalResearch'
        ) as owner:
            owner.return_value.stop.return_value = {'checkpoint_confirmed': True}
            runtime, _ = build_product(root, public_learning=True,
                                       desktop_session_sink=lambda *_: None)
            owner.assert_called_once_with(Path(root).resolve())
            owner.return_value.start.assert_called_once()
            runtime.close()
            owner.return_value.stop.assert_called_once()
            self.assertTrue(runtime.closed)

    def test_failed_start_retires_only_failed_owned_child(self):
        with TemporaryDirectory() as root, patch(
            'mvp.autotrade_mvp.owned_numerical_research.OwnedNumericalResearch'
        ) as owner:
            owner.return_value.start.side_effect = OwnedResearchError('startup failed')
            owner.return_value.stop.side_effect = OwnedResearchError('checkpoint unconfirmed')
            with self.assertRaisesRegex(OwnedResearchError, 'startup failed'):
                build_product(root, public_learning=True, desktop_session_sink=lambda *_: None)
            owner.return_value.terminate_after_failed_stop.assert_called_once()
            # The unserved Host was closed; ordinary offline restart still works.
            runtime, _ = build_product(root, desktop_session_sink=lambda *_: None)
            runtime.close()

    def test_owned_workers_cannot_run_behind_external_status_projection(self):
        with TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'product data directories'):
                build_product(root, public_learning=True,
                              whitebit_learning_state_dir=Path(root) / 'other')
        with self.assertRaises(TypeError):
            build_product('unused', public_learning=1)


if __name__ == '__main__':
    unittest.main()
