"""Plan-10 historical UI remains semantic and refuses invented authority."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "web" / "src" / "index.html"
APP = ROOT / "web" / "src" / "app.js"
DESKTOP_POLICY = ROOT / "src" / "AutoTrade.Desktop" / "WebExperienceSecurityPolicy.cs"


class HistoricalOperatorWebSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.javascript = APP.read_text(encoding="utf-8")
        cls.desktop_policy = DESKTOP_POLICY.read_text(encoding="utf-8")

    def test_surface_reuses_canonical_research_route_and_describes_zero_money(self):
        self.assertEqual(self.html.count('id="historical-operator"'), 1)
        self.assertIn(
            '<section id="historical-operator" aria-labelledby="historical-operator-heading">',
            self.html,
        )
        self.assertIn("simulated USD, never real USD", self.html)
        self.assertIn("1000 simulated USD", self.html)
        self.assertNotIn('href="#historical-operator"', self.html)
        self.assertNotIn("real USD balance", self.html)

    def test_authenticated_backend_actions_are_named_and_start_fail_closed(self):
        for action in ("start", "pause", "stop", "resume", "export"):
            self.assertIn(
                f'id="historical-{action}" type="button" disabled',
                self.html,
            )
        self.assertIn("OWNER-only", self.html)
        self.assertIn('"startHistorical"', self.javascript)
        self.assertIn('HOST_API.route("exportHistorical")', self.javascript)

    def test_status_results_and_trade_stream_are_accessible_and_fail_closed(self):
        for identifier in (
            "historical-session-status",
            "historical-dataset",
            "historical-symbol",
            "historical-strategy",
            "historical-risk-cap",
            "historical-budget",
            "historical-results",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('id="historical-operator-status"', self.html)
        self.assertEqual(self.html.count('id="polite-status" role="status"'), 1)
        self.assertIn(
            'id="historical-trades-region" class="table-scroll" role="region"',
            self.html,
        )
        self.assertIn('aria-label="Historical simulation trade stream" tabindex="0"', self.html)
        self.assertIn(
            "Historical simulation trade stream — simulated values only",
            self.html,
        )
        self.assertIn('id="historical-trades-body"', self.html)
        self.assertIn('colspan="6">No historical simulated trades loaded.', self.html)
        self.assertIn('"historical-trades-region"', self.javascript)

    def test_no_second_browser_or_desktop_command_authority_is_created(self):
        self.assertEqual(self.javascript.count("const HOST_ACTION_ROLES"), 1)
        self.assertEqual(self.javascript.count("async function submitCanonicalCommand"), 1)
        self.assertEqual(self.javascript.count("async function runHistoricalAction"), 1)
        self.assertIn("state.sessionIdentity.role !== \"OWNER\"", self.javascript)

    def test_thin_desktop_forwards_only_generated_historical_routes(self):
        for route in (
            "GetHistoricalStatus", "GetHistoricalResults", "GetHistoricalTrades",
            "StartHistorical", "PauseHistorical", "StopHistorical",
            "ResumeHistorical", "ExportHistorical",
        ):
            self.assertIn("HostApiRoutes." + route, self.desktop_policy)
        self.assertIn('"autotrade-historical-zero.zip"', self.desktop_policy)
        self.assertNotIn('"/api/v1/historical/', self.desktop_policy)


if __name__ == "__main__":
    unittest.main()
