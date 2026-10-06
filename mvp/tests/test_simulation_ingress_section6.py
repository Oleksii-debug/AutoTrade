"""Section-6 canonical simulator public-ingress hardening regressions."""

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.simulation_session import run_canonical_simulation


PRICES = ["100", "101", "103"]
NOW = "2026-10-06T10:00:00Z"


class SimulationIngressSection6Tests(unittest.TestCase):
    def test_episode_id_subclass_is_rejected_before_strip_callback(self):
        class HostileEpisodeId(str):
            callbacks = 0

            def strip(self, *args, **kwargs):
                type(self).callbacks += 1
                raise AssertionError("hostile episode-id strip executed")

        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                TypeError,
                "episode_id must be exact canonical text",
            ):
                run_canonical_simulation(
                    PRICES,
                    directory,
                    episode_id=HostileEpisodeId("episode"),
                    now=NOW,
                )
        self.assertEqual(HostileEpisodeId.callbacks, 0)

    def test_price_list_subclass_is_rejected_before_container_callbacks(self):
        class HostilePrices(list):
            callbacks = 0

            def __bool__(self):
                type(self).callbacks += 1
                raise AssertionError("hostile price-list bool executed")

            def __len__(self):
                type(self).callbacks += 1
                raise AssertionError("hostile price-list len executed")

            def __iter__(self):
                type(self).callbacks += 1
                raise AssertionError("hostile price-list iter executed")

        hostile = HostilePrices(PRICES)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                TypeError,
                "prices must be an exact built-in list",
            ):
                run_canonical_simulation(
                    hostile,
                    directory,
                    episode_id="episode",
                    now=NOW,
                )
        self.assertEqual(HostilePrices.callbacks, 0)

    def test_episode_id_rejects_noncanonical_outer_whitespace(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError,
                "episode_id must be canonical nonempty text",
            ):
                run_canonical_simulation(
                    PRICES,
                    directory,
                    episode_id=" episode ",
                    now=NOW,
                )


if __name__ == "__main__":
    unittest.main()
