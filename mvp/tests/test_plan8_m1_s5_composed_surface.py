"""Plan 8 S5: real canonical ZERO Host serves its one immutable Web UI.

This is provider-free source/integration evidence, not physical NVDA, signed
Windows release, real provider, throughput-on-target-host or trading authority.
Browser keyboard, Desktop package, updater, and load negatives are qualified
as peer jobs on this exact source SHA in provider-free-product.yml.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.tests.test_provider_free_product import ProductClient


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"


class Plan8M1ComposedSurface(unittest.TestCase):
    def test_one_real_host_serves_exact_web_and_financial_state_without_provider(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(Path(directory) / "product")
            try:
                for route, relative in (
                    ("/", "index.html"),
                    ("/index.html", "index.html"),
                    ("/app.js", "app.js"),
                    ("/host-api-routes.js", "host-api-routes.js"),
                    ("/styles.css", "styles.css"),
                ):
                    with self.subTest(route=route):
                        status, text, headers = client.request("GET", route)
                        self.assertEqual(status, 200, route)
                        self.assertEqual(
                            text, (WEB / relative).read_text(encoding="utf-8"),
                            "Host must serve exact canonical Web bytes, not a second UI",
                        )
                        self.assertIn("Content-Security-Policy", headers)
                state = client.state()
                self.assertEqual(state["risk"]["real_order_submission"], "UNAVAILABLE")
                self.assertEqual(state["risk"]["mode"], "ZERO")
                _, outcome = client.command("START_SIMULATION")
                self.assertEqual(outcome["phase"], "SUCCEEDED", outcome)
                completed = client.state()
                self.assertEqual(
                    completed["portfolio"]["status"]["session_status"], "COMPLETED"
                )
                self.assertEqual(completed["risk"]["real_order_submission"], "UNAVAILABLE")
                # A static UI request cannot smuggle a new financial effect.
                immutable_cursor = completed["event_cursor"]
                status, _, _ = client.request("POST", "/", {"action": "START_SIMULATION"})
                self.assertEqual(status, 405)
                status, _, _ = client.request("GET", "/?unexpected=1")
                self.assertEqual(status, 400)
                after = client.state()
                self.assertEqual(after["event_cursor"], immutable_cursor)
                self.assertEqual(
                    after["portfolio"]["orders"], completed["portfolio"]["orders"]
                )
                self.assertEqual(
                    after["portfolio"]["fills"], completed["portfolio"]["fills"]
                )
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
