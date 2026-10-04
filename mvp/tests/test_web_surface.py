import unittest

from mvp.autotrade_mvp.web_surface import (
    command_result_message,
    render_semantic_page,
)


class SemanticWebSurfaceTests(unittest.TestCase):
    def snapshot(self):
        return {
            "state_version": "7",
            "event_cursor": "12",
            "operations": {
                "op-2": "WAITING_EXTERNAL",
                "op-1": "SUCCEEDED",
            },
        }

    def test_document_has_keyboard_and_screen_reader_structure(self):
        html = render_semantic_page(
            self.snapshot(),
            status_text="AutoTrade status\nMode: simulation only",
        )
        self.assertIn('<a href="#main">Skip to main content</a>', html)
        self.assertIn('<main id="main">', html)
        self.assertIn('<h1>AutoTrade local control</h1>', html)
        self.assertIn('<h2 id="status-heading">System status</h2>', html)
        self.assertIn('<label for="action">Action</label>', html)
        self.assertIn('<button type="submit">Submit command</button>', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('aria-live="assertive"', html)

    def test_dynamic_provider_or_status_text_is_escaped(self):
        html = render_semantic_page(
            {"state_version": '1"><script>alert(1)</script>', "event_cursor": "0", "operations": {}},
            status_text="<img src=x onerror=alert(1)>",
            announcement="<script>bad()</script>",
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script&gt;bad()", html)

    def test_operation_state_is_text_not_color_only(self):
        html = render_semantic_page(self.snapshot(), status_text="Ready")
        self.assertIn("<strong>SUCCEEDED</strong>", html)
        self.assertIn("<strong>WAITING_EXTERNAL</strong>", html)

    def test_command_form_carries_exact_state_version(self):
        html = render_semantic_page(self.snapshot(), status_text="Ready")
        self.assertIn(
            'name="expected_state_version" value="7"',
            html,
        )

    def test_acceptance_message_does_not_claim_financial_completion(self):
        message = command_result_message(
            {
                "command_id": "c1",
                "status": "ACCEPTED",
                "operation_id": "o1",
                "reason_codes": [],
            }
        )
        self.assertIn("accepted for processing", message)
        self.assertIn("not yet a completed financial result", message)

    def test_conflict_message_reports_reason(self):
        message = command_result_message(
            {
                "command_id": "c2",
                "status": "CONFLICT",
                "reason_codes": ["stale_state_version"],
            }
        )
        self.assertIn("state conflict", message)
        self.assertIn("stale_state_version", message)


if __name__ == "__main__":
    unittest.main()
