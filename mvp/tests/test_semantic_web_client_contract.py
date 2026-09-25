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

    def test_cursor_gap_recovery_failure_blocks_commands_fail_closed(self):
        js = APP.read_text(encoding="utf-8")
        catch_index = js.index("error.status === 409 || error.status === 410")
        recovery_index = js.index(
            "await refreshSnapshot({announceRefresh: true})",
            catch_index,
        )
        nested_catch = js.index("} catch {", recovery_index)
        blocked = js.index("setCommandAvailability(false)", nested_catch)
        snapshot_unready = js.index("state.snapshotReady = false", nested_catch)
        identity_cleared = js.index("state.sessionIdentity = null", nested_catch)
        self.assertGreater(nested_catch, recovery_index)
        self.assertGreater(blocked, nested_catch)
        self.assertGreater(snapshot_unready, nested_catch)
        self.assertGreater(identity_cleared, nested_catch)
        self.assertIn(
            "Host synchronization gap recovery failed. Commands remain blocked",
            js[nested_catch:blocked + 500],
        )

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
        self.assertIn('version: exactCounter(snapshot.state_version, "state_version")', js)
        self.assertIn('cursor: exactCounter(snapshot.event_cursor, "event_cursor")', js)
        self.assertIn("expected_state_version: state.version.toString()", js)
        self.assertIn("return BigInt(token)", js)
        self.assertNotIn("Number.parseInt(snapshot.state_version", js)
        self.assertNotIn("Number.parseInt(snapshot.event_cursor", js)
        self.assertNotIn("Number(snapshot.state_version", js)
        self.assertNotIn("Number(snapshot.event_cursor", js)

    def test_event_cursor_advances_only_after_required_event_processing(self):
        js = APP.read_text(encoding="utf-8")
        refresh_index = js.index("await refreshOperation(operationId)")
        cursor_index = js.index("state.cursor = cursor", refresh_index)
        version_index = js.index("state.version = version", refresh_index)
        self.assertGreater(cursor_index, refresh_index)
        self.assertGreater(version_index, refresh_index)
        self.assertIn(
            "the next poll retries\n        // the same cursor instead of silently acknowledging",
            js,
        )

    def test_snapshot_counters_cannot_silently_regress(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("const parsed = parseCanonicalSnapshot(snapshot)", js)
        self.assertIn(
            "if (parsed.version < state.version || parsed.cursor < state.cursor)",
            js,
        )
        self.assertIn('throw new Error("host snapshot counters regressed")', js)
        self.assertIn("state.version = parsed.version", js)
        self.assertIn("state.cursor = parsed.cursor", js)

    def test_poll_auto_recovers_snapshot_when_previous_refresh_failed(self):
        js = APP.read_text(encoding="utf-8")
        poll = js.index("async function pollEvents()")
        event_fetch = js.index("${API}/events?after=", poll)
        recovery = js.index("if (!state.snapshotReady)", poll)
        snapshot = js.index("await refreshSnapshot();", recovery)
        self.assertLess(recovery, event_fetch)
        self.assertLess(snapshot, event_fetch)

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
        self.assertIn("expected_state_version: state.version.toString()", js)

    def test_command_identity_comes_only_from_authenticated_host_projection(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn("const actor = permissionSummary.actor", js)
        self.assertIn("const session = permissionSummary.session", js)
        self.assertIn("actor: state.sessionIdentity.actor", js)
        self.assertIn("session: state.sessionIdentity.session", js)
        self.assertIn(
            "if (!state.snapshotReady || state.sessionIdentity === null)",
            js,
        )
        self.assertIn('id="submit-command" type="submit" disabled', html)

    def test_snapshot_uses_only_canonical_ui_snapshot_fields(self):
        js = APP.read_text(encoding="utf-8")
        for required in (
            "snapshot.host_id",
            "snapshot.account_id",
            "snapshot.environment",
            "snapshot.permission_summary",
            "snapshot.connection_freshness",
            "snapshot.portfolio",
            "snapshot.risk",
            "snapshot.strategy",
            "snapshot.jobs",
            "snapshot.reason_codes",
        ):
            self.assertIn(required, js)
        for forbidden in (
            "snapshot.active_host",
            "snapshot.host ??",
            "snapshot.account ??",
            "snapshot.stale",
            "snapshot.ready",
            "snapshot.last_evidence_at",
        ):
            self.assertNotIn(forbidden, js)

    def test_snapshot_environment_and_time_fail_closed_before_commands_enable(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function canonicalEnvironment(value, name)", js)
        self.assertIn('["REPLAY", "SIMULATION", "PAPER", "LIVE"]', js)
        self.assertIn(
            'environment: canonicalEnvironment(snapshot.environment, "environment")',
            js,
        )
        self.assertIn("function utcInstant(value, name)", js)
        self.assertIn(
            'serverTime: utcInstant(snapshot.server_time, "server_time")',
            js,
        )
        self.assertIn(
            'const startedAt = utcInstant(result.started_at, "started_at")',
            js,
        )
        self.assertIn(
            'const updatedAt = utcInstant(result.updated_at, "updated_at")',
            js,
        )

    def test_incomplete_freshness_fails_closed_and_never_claims_current(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            'throw new Error("connection_freshness evidence is required")',
            js,
        )
        self.assertIn("setCommandAvailability(false)", js)
        self.assertIn("Host-provided freshness evidence at ", js)
        self.assertNotIn("Current as of", js)

    def test_command_result_is_validated_against_canonical_contract(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function parseCommandResult(value, expectedCommandId)", js)
        self.assertIn('["ACCEPTED", "REJECTED", "CONFLICT"]', js)
        self.assertIn("CommandResult command_id does not match", js)
        self.assertIn("field_errors must be an array", js)
        self.assertIn("response.status !== 200 && response.status !== 409", js)

    def test_accepted_command_requires_durable_operation_identity(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            'if (result.status === "ACCEPTED" && operationId === null)',
            js,
        )
        self.assertIn(
            "ACCEPTED command must provide operation_id for durable tracking",
            js,
        )

    def test_operation_timestamps_cannot_move_backwards(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            "if (Date.parse(updatedAt) < Date.parse(startedAt))",
            js,
        )
        self.assertIn(
            "OperationResult updated_at cannot precede started_at",
            js,
        )

    def test_accepted_command_tracks_canonical_operation_without_claiming_fill(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn("Remaining uncertainty", html)
        self.assertIn("function parseOperationResult(value, expectedOperationId)", js)
        self.assertIn(
            "${API}/operations/${encodeURIComponent(operationId)}",
            js,
        )
        self.assertIn("OperationResult operation_id does not match", js)
        self.assertIn("OperationResult phase is not canonical", js)
        self.assertIn("renderOperation(operation)", js)
        self.assertIn("await refreshOperation(result.operationId)", js)
        self.assertIn("await refreshOperation(operationId)", js)
        self.assertIn(
            "Current operation status could not be loaded; "
            "the accepted command response remains unchanged.",
            js,
        )
        self.assertIn(
            "It is not yet a completed financial outcome.",
            js,
        )
        self.assertNotIn("innerHTML", js)

    def test_snapshot_refresh_is_local_get_not_invented_host_command(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertNotIn('value="REFRESH_STATE"', html)
        self.assertIn('id="refresh-state" type="button"', html)
        self.assertIn("async function refreshStateFromUser()", js)
        self.assertIn('byId("refresh-state").addEventListener("click", refreshStateFromUser)', js)
        self.assertIn("await refreshSnapshot();", js)
        self.assertNotIn('action: "REFRESH_STATE"', js)

    def test_keyboard_and_high_contrast_rules_are_explicit(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".skip-link:focus", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("@media (forced-colors: active)", css)
        html = INDEX.read_text(encoding="utf-8")
        self.assertNotIn("onclick=", html.lower())
        self.assertNotIn("<canvas", html.lower())


    def test_page_allows_browser_zoom_and_narrow_text_reflow(self):
        html = INDEX.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertIn('name="viewport" content="width=device-width,initial-scale=1"', html)
        self.assertNotIn("maximum-scale", html.lower())
        self.assertNotIn("user-scalable=no", html.lower())
        self.assertNotIn("overflow-x: hidden", css.lower())
        self.assertIn("@media (max-width: 48rem)", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", css)
        self.assertIn("max-inline-size: 100%", css)
        self.assertIn("overflow-wrap: anywhere", css)

    def test_page_restore_restores_only_stable_focus_targets(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn('id="operations-region" class="table-scroll"', html)
        self.assertIn('id="submit-command" type="submit"', html)
        self.assertIn("const RESTORABLE_FOCUS_IDS = new Set([", js)
        self.assertIn("function captureFocusForRestoration()", js)
        self.assertIn("function restoreFocusAfterPageRestore()", js)
        self.assertIn("captureFocusForRestoration();", js)
        self.assertIn("restoreFocusAfterPageRestore();", js)
        self.assertIn("if (!target || target.disabled) return;", js)
        pageshow = js.index('window.addEventListener("pageshow"')
        restore = js.index("restoreFocusAfterPageRestore();", pageshow)
        refresh = js.index("await refreshSnapshot();", pageshow)
        self.assertGreater(restore, refresh)

    def test_operations_table_reflows_inside_keyboard_scroll_region(self):
        html = INDEX.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertIn('id="operations-region" class="table-scroll" role="region"', html)
        self.assertIn('aria-label="Current host operations table" tabindex="0"', html)
        self.assertIn(".table-scroll {", css)
        self.assertIn("overflow-x: auto", css)
        self.assertIn(".table-scroll:focus-visible", css)
        self.assertIn("overflow-wrap: anywhere", css)


    def test_repeated_live_region_message_still_causes_dom_change(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function announceLiveText(id, message)", js)
        self.assertIn('element.textContent = "";', js)
        self.assertIn("window.setTimeout(() => {", js)
        self.assertIn("element.textContent = message;", js)
        self.assertIn('announceLiveText("urgent-status", pending.join(" "))', js)
        self.assertIn(
            'announceLiveText("polite-status", pending.join(" "))',
            js,
        )

    def test_urgent_announcement_bursts_preserve_every_event_in_one_live_update(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("pendingUrgentAnnouncements: []", js)
        self.assertIn("urgentAnnouncementTimer: null", js)
        self.assertIn("state.pendingUrgentAnnouncements.push(message)", js)
        self.assertIn("const pending = state.pendingUrgentAnnouncements", js)
        self.assertIn('announceLiveText("urgent-status", pending.join(" "))', js)
        self.assertNotIn("new Set(state.pendingUrgentAnnouncements)", js)

    def test_polite_aggregation_does_not_drop_identical_material_events(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("const pending = state.pendingAnnouncements", js)
        self.assertIn('announceLiveText("polite-status", pending.join(" "))', js)
        self.assertNotIn("new Set(state.pendingAnnouncements)", js)

    def test_page_restore_blocks_commands_until_fresh_snapshot(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn('window.addEventListener("pagehide"', js)
        self.assertIn('window.addEventListener("pageshow"', js)
        pagehide = js.index('window.addEventListener("pagehide"')
        pageshow = js.index('window.addEventListener("pageshow"')
        self.assertIn("state.snapshotReady = false", js[pagehide:pageshow])
        self.assertIn("state.sessionIdentity = null", js[pagehide:pageshow])
        self.assertIn("setCommandAvailability(false)", js[pagehide:pageshow])
        restored = js[pageshow:]
        self.assertIn("if (!event.persisted) return", restored)
        self.assertIn("state.stopped = false", restored)
        self.assertIn("setCommandAvailability(false)", restored)
        self.assertIn("await refreshSnapshot()", restored)
        self.assertIn("Commands remain blocked", restored)

    def test_unknown_operation_requires_explicit_uncertainty(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            'if (result.phase === "UNKNOWN" && remainingUncertainty.length === 0)',
            js,
        )
        self.assertIn(
            "UNKNOWN operation must preserve explicit remaining uncertainty",
            js,
        )

    def test_terminal_operation_cannot_hide_unresolved_uncertainty(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            '["SUCCEEDED", "FAILED", "CANCELLED"].includes(result.phase)',
            js,
        )
        self.assertIn(
            "Terminal operation cannot retain unresolved uncertainty",
            js,
        )

    def test_ambiguous_command_keeps_exact_identity_for_retry(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("pendingCommand: null", js)
        self.assertIn("function commandForSubmission(action)", js)
        self.assertIn("if (state.pendingCommand !== null)", js)
        self.assertIn("return state.pendingCommand", js)
        self.assertIn("state.pendingCommand = payload", js)
        self.assertIn("const payload = commandForSubmission(action)", js)
        self.assertIn("clearConfirmedCommand(payload)", js)
        self.assertIn(
            "Its original command_id and idempotency_key are retained for exact retry",
            js,
        )
        submit = js.index("async function submitCommand(event)")
        construct = js.index("const payload = commandForSubmission(action)", submit)
        post = js.index("await submitCanonicalCommand(payload)", construct)
        clear = js.index("clearConfirmedCommand(payload)", post)
        ambiguous = js.index("could not be confirmed", clear)
        self.assertLess(construct, post)
        self.assertLess(post, clear)
        self.assertLess(clear, ambiguous)

    def test_pending_command_retry_does_not_mint_new_identifiers_or_new_state_version(self):
        js = APP.read_text(encoding="utf-8")
        helper = js[js.index("function commandForSubmission(action)"):]
        helper = helper[:helper.index("function clearConfirmedCommand")]
        pending_check = helper.index("if (state.pendingCommand !== null)")
        pending_return = helper.index("return state.pendingCommand")
        uuid_mint = helper.index("newCommandPayload(action)")
        self.assertLess(pending_check, pending_return)
        self.assertLess(pending_return, uuid_mint)
        self.assertNotIn("expected_state_version =", helper)
        self.assertIn(
            "with its original idempotency identity. No new command is being created.",
            js,
        )

if __name__ == "__main__":
    unittest.main()
