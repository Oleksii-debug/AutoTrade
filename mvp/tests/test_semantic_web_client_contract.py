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

    def test_canonical_snapshot_projections_are_exposed_as_semantic_tables(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn('href="#opportunities">Opportunities and decisions</a>', html)
        for required in (
            '<caption>Authenticated permission and capability evidence</caption>',
            '<caption>Strategy and decision projection</caption>',
            '<caption>Portfolio projection</caption>',
            '<caption>Risk and authority projection</caption>',
            '<caption>Background research and replay jobs</caption>',
            'id="permissions-body"',
            'id="strategy-body"',
            'id="portfolio-body"',
            'id="risk-body"',
            'id="jobs-body"',
            'id="server-time"',
        ):
            self.assertIn(required, html)
        self.assertIn("function renderProjection(bodyId, record, emptyMessage)", js)
        self.assertIn("function renderPermissionSummary(permissionSummary)", js)\n        self.assertIn("renderPermissionSummary(parsed.permissionSummary)", js)
        self.assertIn('renderProjection(\n      "portfolio-body"', js)
        self.assertIn('renderProjection(\n      "risk-body"', js)
        self.assertIn('renderProjection(\n      "strategy-body"', js)
        self.assertIn("renderJobs(parsed.jobs)", js)
        self.assertIn('text("server-time", parsed.serverTime)', js)
        self.assertNotIn("Not loaded.", html)

    def test_projection_rendering_is_text_only_deterministic_and_focusable(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function stableProjectionValue(value)", js)
        self.assertIn("Object.keys(value).sort()", js)
        self.assertIn("rows.push([path, projectionText(value)])", js)\n        self.assertIn("cell.textContent = value", js)
        self.assertIn('header.scope = "row"', js)
        self.assertNotIn("innerHTML", js)
        for region in (
            "permissions-region",
            "strategy-region",
            "portfolio-region",
            "risk-region",
            "jobs-region",
        ):
            self.assertIn(f'id="{region}" class="table-scroll" role="region"', html)
            self.assertIn(f'"{region}"', js)

    def test_received_host_events_are_exposed_as_read_only_semantic_history(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            '<caption>Canonical host event history received by this session</caption>',
            html,
        )
        self.assertIn(
            'id="event-history-region" class="table-scroll" role="region"',
            html,
        )
        self.assertIn('id="event-history-body"', html)
        self.assertIn(
            "missing events are not invented or reconstructed in the browser",
            html,
        )
        self.assertIn('"event-history-region"', js)
        self.assertIn("function renderHostEvent(event, cursor, stateVersion)", js)
        self.assertIn("row.children[0].textContent = cursor.toString()", js)
        self.assertIn("row.children[1].textContent = stateVersion.toString()", js)
        self.assertIn("row.children[2].textContent = kind", js)
        self.assertIn("row.children[3].textContent = projectionText(payload)", js)
        self.assertIn("while (body.children.length > 100)", js)
        self.assertNotIn("innerHTML", js)

    def test_account_or_environment_scope_change_clears_history_and_counter_baseline(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function resetEventHistoryForScope()", js)
        self.assertIn("const scopeChanged = state.accountId !== null", js)
        self.assertIn("parsed.accountId !== state.accountId", js)
        self.assertIn("parsed.environment !== state.environment", js)
        scope = js.index("if (scopeChanged)")
        cursor_reset = js.index("state.cursor = 0n", scope)
        version_reset = js.index("state.version = 0n", scope)
        history_reset = js.index("resetEventHistoryForScope()", scope)
        regression_check = js.index("host snapshot counters regressed", scope)
        self.assertLess(cursor_reset, regression_check)
        self.assertLess(version_reset, regression_check)
        self.assertLess(history_reset, regression_check)
        self.assertIn(
            "No canonical host events received in this account/environment session.",
            js,
        )

    def test_event_history_is_recorded_only_after_required_event_processing(self):
        js = APP.read_text(encoding="utf-8")
        poll = js.index("async function pollEvents()")
        refresh = js.index("await refreshOperation(operationId)", poll)
        history = js.index("renderHostEvent(event, cursor, version)", refresh)
        cursor_commit = js.index("state.cursor = cursor", history)
        self.assertLess(refresh, history)
        self.assertLess(history, cursor_commit)

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

    def test_v2_command_copies_exact_scope_from_fresh_host_snapshot(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("accountId: null", js)
        self.assertIn("environment: null", js)
        self.assertIn("state.accountId = parsed.accountId", js)
        self.assertIn("state.environment = parsed.environment", js)
        self.assertIn("account_id: state.accountId", js)
        self.assertIn("environment: state.environment", js)
        render = js.index("function renderSnapshot(snapshot")
        ready = js.index("state.snapshotReady = true", render)
        account = js.index("state.accountId = parsed.accountId", render)
        environment = js.index("state.environment = parsed.environment", render)
        self.assertLess(account, ready)
        self.assertLess(environment, ready)

    def test_unresolved_command_cannot_be_retargeted_after_scope_or_session_change(self):
        js = APP.read_text(encoding="utf-8")
        submit = js.index("async function submitCommand(event)")
        build = js.index("const payload = commandForSubmission(action)", submit)
        fence = js.index("if (recovering && (", submit)
        self.assertLess(fence, build)
        for required in (
            "state.pendingCommand.actor !== state.sessionIdentity.actor",
            "state.pendingCommand.session !== state.sessionIdentity.session",
            "state.pendingCommand.account_id !== state.accountId",
            "state.pendingCommand.environment !== state.environment",
            "The original command identity is preserved and will not be retargeted.",
        ):
            self.assertIn(required, js[fence:build])
        self.assertIn("setCommandAvailability(false)", js[fence:build])

    def test_snapshot_trust_invalidation_clears_account_and_environment_scope(self):
        js = APP.read_text(encoding="utf-8")
        self.assertGreaterEqual(js.count("state.accountId = null"), 4)
        self.assertGreaterEqual(js.count("state.environment = null"), 4)
        pagehide = js.index('window.addEventListener("pagehide"')
        pageshow = js.index('window.addEventListener("pageshow"')
        self.assertIn("state.accountId = null", js[pagehide:pageshow])
        self.assertIn("state.environment = null", js[pagehide:pageshow])

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

    def test_host_safety_commands_fail_closed_by_authenticated_role(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            'BLOCK_NEW_EXPOSURE: new Set(["OWNER", "OPERATOR"])',
            js,
        )
        self.assertIn(
            'REVOKE_AUTHORITY: new Set(["OWNER"])',
            js,
        )
        self.assertIn(
            'role: requiredText(permissionSummary.role, "permission_summary.role")',
            js,
        )
        self.assertIn("function roleCanSubmitAction(role, action)", js)
        self.assertIn("function syncHostActionOptions(role)", js)
        self.assertIn("option.disabled = !allowed", js)
        self.assertIn(
            "The authenticated role has no permitted host safety command. "
            "Commands remain blocked.",
            js,
        )
        self.assertIn('value="BLOCK_NEW_EXPOSURE"', html)
        self.assertIn('value="REVOKE_AUTHORITY"', html)
        self.assertNotIn('value="SET_AUTHORITY"', html)

    def test_action_change_rechecks_role_before_enabling_submit(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            'byId("host-action").addEventListener("change", () => {',
            js,
        )
        self.assertIn(
            "const effectiveAction = state.pendingCommand !== null",
            js,
        )
        self.assertIn(
            "roleCanSubmitAction(state.sessionIdentity.role, effectiveAction)",
            js,
        )
        self.assertIn(
            "setCommandAvailability(state.snapshotReady && "
            "state.sessionIdentity !== null);",
            js,
        )

    def test_pending_owner_command_is_not_retried_after_role_downgrade(self):
        js = APP.read_text(encoding="utf-8")
        submit = js.index("async function submitCommand(event)")
        payload = js.index("const payload = commandForSubmission(action)", submit)
        role_fence = js.index(
            "if (recovering && !roleCanSubmitAction(",
            submit,
        )
        self.assertLess(role_fence, payload)
        self.assertIn(
            "state.pendingCommand.action",
            js[role_fence:payload],
        )
        self.assertIn(
            "The authenticated role no longer permits the unresolved command.",
            js[role_fence:payload],
        )
        self.assertIn(
            "the browser will not retry it.",
            js[role_fence:payload],
        )
        self.assertIn("setCommandAvailability(false)", js[role_fence:payload])

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

    def test_permission_summary_matches_canonical_host_contract(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function parsePermissionSummary(value)", js)
        self.assertIn(
            'const allowed = new Set(["actor", "session", "role", "capabilities"])',
            js,
        )
        self.assertIn(
            "permission_summary contains non-canonical field",
            js,
        )
        self.assertIn(
            "if (!/^sid-[0-9a-f]{64}$/.test(session))",
            js,
        )
        self.assertIn(
            "permission_summary.session must be a canonical public session reference",
            js,
        )
        self.assertIn(
            "permission_summary.capabilities must contain unique canonical strings",
            js,
        )
        self.assertIn(
            "const permissionSummary = parsePermissionSummary(",
            js,
        )
        self.assertNotIn(
            "if (actor === undefined && session === undefined) return null",
            js,
        )

    def test_nested_host_projection_is_exposed_as_stable_semantic_rows(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function flattenProjectionRows(record)", js)
        self.assertIn(
            'visit(item, path + "[" + String(index + 1) + "]")',
            js,
        )
        self.assertIn(
            'visit(value[key], path ? path + "." + key : key)',
            js,
        )
        self.assertIn("const entries = flattenProjectionRows(record)", js)
        render = js[js.index("function renderProjection("):]
        render = render[:render.index("function renderPermissionSummary(")]
        self.assertNotIn("Object.entries(record)", render)
        self.assertNotIn("JSON.stringify(value)", render)

    def test_permission_capabilities_are_individual_keyboard_readable_rows(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            'aria-label="Authenticated permission and capability evidence"',
            html,
        )
        self.assertIn("function renderPermissionSummary(permissionSummary)", js)
        self.assertIn(
            'appendProjectionRow(body, "Actor", permissionSummary.actor)',
            js,
        )
        self.assertIn(
            'appendProjectionRow(body, "Session", permissionSummary.session)',
            js,
        )
        self.assertIn(
            'appendProjectionRow(body, "Role", permissionSummary.role)',
            js,
        )
        self.assertIn(
            'body, "Capability " + String(index + 1), capability',
            js,
        )
        self.assertIn("renderPermissionSummary(parsed.permissionSummary)", js)


if __name__ == "__main__":
    unittest.main()
