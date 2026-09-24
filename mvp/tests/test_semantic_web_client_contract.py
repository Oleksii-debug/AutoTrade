from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "web" / "src" / "index.html"
APP = ROOT / "web" / "src" / "app.js"
CSS = ROOT / "web" / "src" / "styles.css"


class SemanticWebClientContractTests(unittest.TestCase):
    def test_page_has_landmarks_skip_link_labels_and_table_headers(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn('href="#main">Skip to main content</a>', html)
        self.assertIn('<nav aria-label="Primary">', html)
        self.assertIn('<main id="main" tabindex="-1">', html)
        self.assertIn('<caption>Current host operations</caption>', html)
        self.assertGreaterEqual(html.count('scope="col"'), 2)
        self.assertIn('<label for="host-action">Action</label>', html)

    def test_material_notifications_are_separate_from_market_tick_noise(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("const MATERIAL_EVENTS = new Set([", js)
        self.assertIn('"FILL_RECORDED"', js)
        self.assertIn('"PROVIDER_DEGRADED"', js)
        self.assertIn('"AUTHORITY_REVOKED"', js)
        self.assertIn('if (kind === "AUTHORITY_REVOKED")', js)
        self.assertNotIn('"MARKET_TICK"', js)
        self.assertIn("state.pendingAnnouncements.push(message)", js)
        self.assertIn("window.setTimeout", js)
        self.assertIn("while (history.children.length > 50)", js)

    def test_event_gap_forces_snapshot_instead_of_inventing_continuity(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("error.status === 409 || error.status === 410", js)
        self.assertIn("refreshSnapshot({announceRefresh: true})", js)
        self.assertIn("Host state snapshot refreshed after an event cursor gap.", js)

    def test_successful_http_response_still_rejects_internal_cursor_gap(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("let expectedCursor = state.cursor + 1n", js)
        self.assertIn("if (cursor !== expectedCursor)", js)
        self.assertIn("expectedCursor = cursor + 1n", js)
        self.assertIn("if (version < state.version)", js)
        self.assertGreaterEqual(
            js.count("refreshSnapshot({announceRefresh: true})"),
            3,
        )

    def test_host_requests_use_same_origin_session_and_no_embedded_secret(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn('credentials: "same-origin"', js)
        self.assertIn('cache: "no-store"', js)
        for forbidden in ("api_key", "client_secret", "access_token", "Bearer "):
            self.assertNotIn(forbidden, js)

    def test_state_versions_and_event_cursors_never_use_lossy_javascript_numbers(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function exactCounter(value, name)", js)
        self.assertIn("Number.isSafeInteger(value)", js)
        self.assertIn("return BigInt(token)", js)
        self.assertIn("state.version = exactCounter(", js)
        self.assertIn("state.cursor = exactCounter(", js)
        self.assertIn("expected_state_version: state.version.toString()", js)
        self.assertNotIn("Number.parseInt(snapshot.state_version", js)
        self.assertNotIn("Number.parseInt(snapshot.event_cursor", js)

    def test_snapshot_counters_cannot_silently_regress(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("const nextVersion = exactCounter(", js)
        self.assertIn("const nextCursor = exactCounter(", js)
        self.assertIn(
            "if (nextVersion < state.version || nextCursor < state.cursor)",
            js,
        )
        self.assertIn('throw new Error("host snapshot counters regressed")', js)

    def test_confirmed_command_response_survives_snapshot_refresh_failure(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            "Host state refresh failed after the command response. "
            "The confirmed command response remains unchanged.",
            js,
        )
        self.assertIn("await refreshSnapshot();", js)

    def test_command_result_does_not_equate_acceptance_with_completion(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn("Accepted commands are not fills.", html)
        self.assertIn("not yet a completed financial outcome", js)
        self.assertIn(
            "No durable financial or safety outcome is being claimed",
            js,
        )
        self.assertIn("expected_state_version: String(state.version)", js)

    def test_keyboard_and_high_contrast_rules_are_explicit(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".skip-link:focus", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("@media (forced-colors: active)", css)
        html = INDEX.read_text(encoding="utf-8")
        self.assertNotIn("onclick=", html.lower())
        self.assertNotIn("<canvas", html.lower())


if __name__ == "__main__":
    unittest.main()
