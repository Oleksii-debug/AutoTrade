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

    def test_mixed_operation_id_types_do_not_crash_accessible_surface(self):
        html = render_semantic_page(
            {
                "state_version": "7",
                "event_cursor": "12",
                "operations": {2: "WAITING_EXTERNAL", "op-1": "SUCCEEDED"},
            },
            status_text="Ready",
        )
        self.assertIn('<span class="operation-id">2</span>', html)
        self.assertIn('<span class="operation-id">op-1</span>', html)
        self.assertIn("<strong>WAITING_EXTERNAL</strong>", html)
        self.assertIn("<strong>SUCCEEDED</strong>", html)

    def test_hostile_operation_values_cannot_crash_accessible_surface(self):
        class HostileText:
            def __str__(self):
                raise AssertionError("malformed state must not execute __str__")

        html = render_semantic_page(
            {
                "state_version": "7",
                "event_cursor": "12",
                "operations": {
                    HostileText(): "WAITING_EXTERNAL",
                    "op-1": HostileText(),
                },
            },
            status_text="Ready",
        )

        self.assertIn('<span class="operation-id">Unavailable</span>', html)
        self.assertIn('<span class="operation-id">op-1</span>', html)
        self.assertIn("<strong>Unavailable</strong>", html)
        self.assertIn("<strong>WAITING_EXTERNAL</strong>", html)

    def test_malformed_status_text_is_announced_explicitly(self):
        class HostileText:
            def __str__(self):
                raise AssertionError("status rendering must not execute __str__")

        html = render_semantic_page(
            {"state_version": "7", "event_cursor": "12", "operations": {}},
            status_text=HostileText(),
        )
        self.assertIn("Status details are unavailable.", html)

    def test_operation_mapping_subclass_is_rejected_without_iteration(self):
        class HostileOperations(dict):
            def items(self):
                raise AssertionError("malformed operations must not execute overridden items")

        html = render_semantic_page(
            {
                "state_version": "7",
                "event_cursor": "12",
                "operations": HostileOperations({"op-1": "SUCCEEDED"}),
            },
            status_text="Ready",
        )
        self.assertIn(
            "Operation state is unavailable because the state shape is malformed.",
            html,
        )
        self.assertNotIn("op-1", html)

    def test_hostile_top_level_snapshot_fails_closed_without_get(self):
        class HostileSnapshot(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("malformed snapshot must not execute overridden get")

        html = render_semantic_page(
            HostileSnapshot(
                {
                    "state_version": "999",
                    "event_cursor": "999",
                    "operations": {"forged": "SUCCEEDED"},
                }
            ),
            status_text="Status remains available",
        )

        self.assertIn("Status remains available", html)
        self.assertIn("<dt>State version</dt><dd>Unavailable</dd>", html)
        self.assertIn("<dt>Event cursor</dt><dd>Unavailable</dd>", html)
        self.assertIn(
            "Operation state is unavailable because the state shape is malformed.",
            html,
        )
        self.assertIn(
            "Commands are unavailable because the current state version is malformed or unavailable.",
            html,
        )
        self.assertNotIn("<form", html)
        self.assertNotIn('name="expected_state_version"', html)
        self.assertNotIn("forged", html)


    def test_nonfinite_scalars_do_not_render_as_status_values(self):
        html = render_semantic_page(
            {
                "state_version": float("nan"),
                "event_cursor": float("inf"),
                "operations": {},
            },
            status_text="Ready",
        )
        self.assertEqual(html.count("<dd>Unavailable</dd>"), 2)
        self.assertIn(
            "Commands are unavailable because the current state version is malformed or unavailable.",
            html,
        )
        self.assertNotIn("<form", html)
        self.assertNotIn(">nan<", html.lower())
        self.assertNotIn(">inf<", html.lower())

    def test_hostile_command_result_container_fails_closed_without_get(self):
        class HostileResult(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("malformed command result must not execute get")

        message = command_result_message(HostileResult({"status": "ACCEPTED"}))
        self.assertEqual(
            message,
            "Command result is unavailable because the result shape is malformed.",
        )

    def test_hostile_reason_sequence_is_not_iterated(self):
        class HostileReasons(list):
            def __iter__(self):
                raise AssertionError("malformed reason sequence must not be iterated")

        message = command_result_message(
            {
                "status": "CONFLICT",
                "command_id": "cmd-1",
                "reason_codes": HostileReasons(["STALE_STATE"]),
            }
        )
        self.assertIn("Command cmd-1 was not accepted", message)
        self.assertIn("Reasons: none.", message)

    def test_command_form_carries_exact_state_version(self):
        html = render_semantic_page(self.snapshot(), status_text="Ready")
        self.assertIn(
            'name="expected_state_version" value="7"',
            html,
        )

    def test_malformed_state_version_keeps_status_readable_but_disables_commands(self):
        for value in (True, 7, 7.0, -1, "", "007", "latest", "１２"):
            with self.subTest(value=value):
                snapshot = self.snapshot()
                snapshot["state_version"] = value
                html = render_semantic_page(snapshot, status_text="Ready")
                self.assertIn("Ready", html)
                self.assertIn("<dt>State version</dt><dd>Unavailable</dd>", html)
                self.assertIn(
                    "Commands are unavailable because the current state version is malformed or unavailable.",
                    html,
                )
                self.assertNotIn("<form", html)
                self.assertNotIn('name="expected_state_version"', html)
                self.assertNotIn("<button", html)

    def test_malformed_event_cursor_does_not_corrupt_command_state_authority(self):
        snapshot = self.snapshot()
        snapshot["event_cursor"] = True
        html = render_semantic_page(snapshot, status_text="Ready")
        self.assertIn("<dt>Event cursor</dt><dd>Unavailable</dd>", html)
        self.assertIn(
            'name="expected_state_version" value="7"',
            html,
        )

    def test_zero_state_version_is_canonical_and_commandable(self):
        snapshot = self.snapshot()
        snapshot["state_version"] = "0"
        html = render_semantic_page(snapshot, status_text="Ready")
        self.assertIn(
            'name="expected_state_version" value="0"',
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
