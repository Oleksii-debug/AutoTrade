from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "web" / "src" / "index.html"
APP = ROOT / "web" / "src" / "app.js"
CSS = ROOT / "web" / "src" / "styles.css"


class _ElementParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


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
        self.assertIn("function renderPermissionSummary(permissionSummary)", js)
        self.assertIn("renderPermissionSummary(parsed.permissionSummary)", js)
        self.assertIn('renderProjection(\n      "portfolio-body"', js)
        self.assertIn('renderProjection(\n      "risk-body"', js)
        self.assertIn('renderProjection(\n      "strategy-body"', js)
        self.assertIn("renderJobs(parsed.jobs)", js)
        self.assertIn('text("server-time", parsed.serverTime)', js)
        self.assertNotIn("Not loaded.", html)

    def test_table_filters_clear_with_escape_without_moving_focus(self):
        js = APP.read_text(encoding="utf-8")
        bind = js[
            js.index("function bindTableTools()"):
            js.index("function announceLiveText")
        ]
        self.assertIn('filter.addEventListener("keydown", (event) => {', bind)
        self.assertIn('event.key !== "Escape" || filter.value === ""', bind)
        self.assertIn("event.preventDefault()", bind)
        self.assertIn('filter.value = ""', bind)
        self.assertIn("applyTableFilter(tool, {resetPage: true})", bind)
        self.assertNotIn("filter.blur()", bind)
        html = INDEX.read_text(encoding="utf-8")
        for filter_id in (
            "permissions-filter",
            "strategy-filter",
            "portfolio-filter",
            "operations-filter",
            "risk-filter",
            "jobs-filter",
            "event-history-filter",
        ):
            self.assertIn(
                f'id="{filter_id}" type="search" aria-keyshortcuts="Escape"',
                html,
            )
        for region_id, status_id in (
            ("permissions-region", "permissions-filter-status"),
            ("strategy-region", "strategy-filter-status"),
            ("portfolio-region", "portfolio-filter-status"),
            ("operations-region", "operations-filter-status"),
            ("risk-region", "risk-filter-status"),
            ("jobs-region", "jobs-filter-status"),
            ("event-history-region", "event-history-filter-status"),
        ):
            self.assertRegex(
                html,
                rf'id="{region_id}"[^>]*aria-describedby="{status_id}"',
            )

    def test_projection_rendering_is_text_only_deterministic_and_focusable(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function stableProjectionValue(value)", js)
        self.assertIn("Object.keys(value).sort()", js)
        self.assertIn("rows.push([path, projectionText(value)])", js)
        self.assertIn("cell.textContent = value", js)
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

    def test_dynamic_operation_and_event_tables_use_row_headers(self):
        js = APP.read_text(encoding="utf-8")
        operation = js[
            js.index("function renderOperation(operation)"):
            js.index("async function refreshOperation")
        ]
        event = js[
            js.index("function renderHostEvent(event, cursor, stateVersion)"):
            js.index("function resetOperationsForScope(")
        ]
        for scope in (operation, event):
            self.assertIn('const rowHeader = document.createElement("th")', scope)
            self.assertIn('rowHeader.scope = "row"', scope)
            self.assertIn("row.appendChild(rowHeader)", scope)
            self.assertIn("for (let index = 1; index < 4; index += 1)", scope)
        self.assertIn("row.children[0].textContent = operation.operationId", operation)
        self.assertIn("row.children[0].textContent = cursor.toString()", event)

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
        event = js[js.index("function renderHostEvent"):js.index("function resetNotificationsForScope")]
        self.assertIn('const rowHeader = document.createElement("th")', event)
        self.assertIn('rowHeader.scope = "row"', event)
        self.assertIn("for (const expired of retained.slice(100)) expired.remove();", event)
        self.assertNotIn("innerHTML", js)

    def test_account_or_environment_scope_change_clears_history_and_counter_baseline(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function resetEventHistoryForScope(", js)
        self.assertIn("renderedAccountId: null", js)
        self.assertIn("renderedEnvironment: null", js)
        self.assertIn("const scopeChanged = state.renderedAccountId !== null", js)
        self.assertIn("parsed.accountId !== state.renderedAccountId", js)
        self.assertIn("parsed.environment !== state.renderedEnvironment", js)
        self.assertIn("state.renderedAccountId = parsed.accountId", js)
        self.assertIn("state.renderedEnvironment = parsed.environment", js)
        scope = js.index("if (displayContextChanged)")
        cursor_reset = js.index("state.cursor = 0n", scope)
        version_reset = js.index("state.version = 0n", scope)
        history_reset = js.index("resetEventHistoryForScope();", scope)
        regression_check = js.index("host snapshot counters regressed", scope)
        self.assertLess(cursor_reset, regression_check)
        self.assertLess(version_reset, regression_check)
        self.assertLess(history_reset, regression_check)
        self.assertIn(
            "No canonical host events received in this account/environment session.",
            js,
        )

    def test_scope_and_cursor_evidence_resets_discard_old_context_speech_queue(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("announcementGeneration: 0", js)

        discard = js[
            js.index("function discardQueuedAnnouncementsForEvidenceReset"):
            js.index("function queuePoliteAnnouncement")
        ]
        self.assertIn("state.announcementGeneration += 1", discard)
        self.assertIn("window.clearTimeout(state.announcementTimer)", discard)
        self.assertIn("window.clearTimeout(state.urgentAnnouncementTimer)", discard)
        self.assertIn("state.pendingAnnouncements = []", discard)
        self.assertIn("state.pendingUrgentAnnouncements = []", discard)
        self.assertIn('text("polite-status", "")', discard)
        self.assertIn('text("urgent-status", "")', discard)

        live = js[
            js.index("function announceLiveText"):
            js.index("function discardQueuedAnnouncementsForEvidenceReset")
        ]
        self.assertIn("const generation = state.announcementGeneration", live)
        self.assertIn("generation === state.announcementGeneration", live)

        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        scope = snapshot.index("if (displayContextChanged)")
        scope_discard = snapshot.index(
            "discardQueuedAnnouncementsForEvidenceReset();", scope)
        scope_history = snapshot.index("resetNotificationsForScope();", scope)
        self.assertLess(scope_discard, scope_history)

        gap = snapshot.index("if (skippedSameScopeEvents)")
        gap_discard = snapshot.index(
            "discardQueuedAnnouncementsForEvidenceReset();", gap)
        gap_history = snapshot.index("resetNotificationsForScope(", gap)
        self.assertLess(gap_discard, gap_history)

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

    def test_state_versions_and_event_cursors_require_canonical_sequence_strings(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function exactCounter(value, name)", js)
        self.assertIn('typeof value !== "string"', js)
        self.assertIn("/^(0|[1-9][0-9]*)$/.test(value)", js)
        self.assertIn("return BigInt(value)", js)
        self.assertIn('version: exactCounter(snapshot.state_version, "state_version")', js)
        self.assertIn('cursor: exactCounter(snapshot.event_cursor, "event_cursor")', js)
        self.assertIn("expected_state_version: state.version.toString()", js)
        self.assertNotIn("Number.isSafeInteger(value)", js)
        self.assertNotIn("const token = String(value)", js)
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

    def test_host_event_retry_is_cursor_idempotent_and_conflicting_reuse_fails_closed(self):
        js = APP.read_text(encoding="utf-8")
        render = js[
            js.index("function renderHostEvent"):
            js.index("function resetNotificationsForScope")
        ]
        self.assertIn("candidate.dataset.hostEventCursor === cursorText", render)
        self.assertIn("matchingRows.length > 1", render)
        self.assertIn("received host-event history contains a duplicate cursor", render)
        self.assertIn("host event cursor was reused with conflicting rendered content", render)
        self.assertIn('row.dataset.selectionKey !== "event:" + cursorText', render)
        self.assertEqual(render.count("body.prepend(row)"), 1)
        self.assertIn("BigInt(left.dataset.hostEventCursor)", render)
        self.assertIn("retained.slice(100)", render)

        announce = js[
            js.index("function announce(message"):
            js.index("async function jsonFetch")
        ]
        self.assertIn("historyKey = null", announce)
        self.assertIn("item.dataset.notificationKey === normalizedHistoryKey", announce)
        self.assertIn("item.dataset.notificationKey = normalizedHistoryKey", announce)

        poll = js[
            js.index("async function pollEvents()"):
            js.index("function requiredPolicyInput")
        ]
        self.assertIn('"event:" + cursor.toString()', poll)

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
        event_fetch = js.index('HOST_API.route("streamEvents") + "?after="', poll)
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
        build = js.index("payload = commandForSubmission(action)", submit)
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

    def test_display_scope_marker_survives_authority_trust_invalidation(self):
        js = APP.read_text(encoding="utf-8")
        pagehide = js.index('window.addEventListener("pagehide"')
        pageshow = js.index('window.addEventListener("pageshow"')
        invalidation = js[pagehide:pageshow]
        self.assertIn("state.accountId = null", invalidation)
        self.assertIn("state.environment = null", invalidation)
        self.assertNotIn("state.renderedAccountId = null", invalidation)
        self.assertNotIn("state.renderedEnvironment = null", invalidation)
        self.assertIn(
            "successfully rendered scope is retained only to prevent stale read-only",
            js,
        )

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

    def test_snapshot_jobs_reject_non_object_entries_before_render(self):
        js = APP.read_text(encoding="utf-8")
        parser = js[
            js.index("function parseCanonicalSnapshot"):
            js.index("function parseCommandResult")
        ]
        self.assertIn("!Array.isArray(snapshot.jobs)", parser)
        self.assertIn("snapshot.jobs.some((item) =>", parser)
        self.assertIn(
            '!item || typeof item !== "object" || Array.isArray(item)',
            parser,
        )
        self.assertIn(
            'throw new Error("jobs must be an array of objects")',
            parser,
        )

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
        self.assertIn("field_errors must be an array of objects", js)
        self.assertIn('result.field_errors.some((item) =>', js)
        self.assertIn(
            '!item || typeof item !== "object" || Array.isArray(item)',
            js,
        )
        self.assertIn("response.status !== 200 && response.status !== 409", js)

    def test_command_field_validation_details_are_semantic_and_never_silently_dropped(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn(
            'id="command-result" tabindex="-1" aria-describedby="command-validation-details"',
            html,
        )
        self.assertIn(
            'id="command-validation-details" role="region" aria-labelledby="command-validation-heading"',
            html,
        )
        self.assertIn(
            '<h3 id="command-validation-heading">Command validation details</h3>',
            html,
        )
        self.assertIn('id="command-validation-list"', html)
        self.assertIn(
            'function renderCommandValidationDetails(fieldErrors, stateName)',
            js,
        )
        self.assertIn('appendMessage(projectionText(error))', js)
        self.assertIn(
            'renderCommandValidationDetails(result.fieldErrors, "confirmed")',
            js,
        )
        self.assertIn(
            'renderCommandValidationDetails([], "pending")',
            js,
        )
        self.assertIn(
            'renderCommandValidationDetails([], "unavailable")',
            js,
        )
        self.assertIn(
            "No field-specific validation errors reported by the host.",
            js,
        )
        self.assertEqual(js.count("innerHTML"), 0)

        submit = js.index("async function submitCommand(event)")
        pending = js.index('renderCommandValidationDetails([], "pending")', submit)
        network = js.index("await submitCanonicalCommand(payload)", submit)
        confirmed = js.index(
            'renderCommandValidationDetails(result.fieldErrors, "confirmed")',
            network,
        )
        status_branch = js.index('if (result.status === "ACCEPTED")', confirmed)
        unavailable = js.index(
            'renderCommandValidationDetails([], "unavailable")',
            status_branch,
        )
        self.assertLess(pending, network)
        self.assertLess(confirmed, status_branch)
        self.assertGreater(unavailable, status_branch)

    def test_operation_identity_is_canonical_uuid_before_route_use(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function canonicalId(value, name)", js)
        self.assertIn(
            "/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/",
            js,
        )
        self.assertIn(
            'const commandId = canonicalId(result.command_id, "command_id")',
            js,
        )
        self.assertIn(
            ': canonicalId(result.operation_id, "operation_id")',
            js,
        )
        self.assertIn(
            'const operationId = canonicalId(result.operation_id, "operation_id")',
            js,
        )
        parse_command = js.index("function parseCommandResult(value, expectedCommandId)")
        command_route = js.index('HOST_API.route("getOperation"', parse_command)
        self.assertLess(
            js.index('canonicalId(result.operation_id, "operation_id")', parse_command),
            command_route,
        )

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
        self.assertIn("function compareCanonicalUtcInstants(", js)
        self.assertIn('leftFraction.padEnd(width, "0")', js)
        self.assertIn('rightFraction.padEnd(width, "0")', js)
        operation = js[
            js.index("function parseOperationResult(value, expectedOperationId)"):
            js.index("function setCommandAvailability")
        ]
        self.assertIn(
            "if (compareCanonicalUtcInstants(",
            operation,
        )
        self.assertNotIn("Date.parse(", operation)
        self.assertIn(
            '"updated_at",\n        "started_at") < 0',
            operation,
        )
        self.assertIn(
            "OperationResult updated_at cannot precede started_at",
            operation,
        )


    def test_exact_utc_comparator_preserves_submillisecond_ordering(self):
        js = APP.read_text(encoding="utf-8")
        compare = js[
            js.index("function compareCanonicalUtcInstants("):
            js.index("function requiredStringArray")
        ]
        self.assertIn("if (leftBase < rightBase) return -1", compare)
        self.assertIn("if (leftBase > rightBase) return 1", compare)
        self.assertIn('leftFraction.padEnd(width, "0")', compare)
        self.assertIn('rightFraction.padEnd(width, "0")', compare)
        self.assertIn("if (normalizedLeft < normalizedRight) return -1", compare)
        self.assertIn("if (normalizedLeft > normalizedRight) return 1", compare)


    def test_accepted_command_tracks_canonical_operation_without_claiming_fill(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn("Remaining uncertainty", html)
        self.assertIn("function parseOperationResult(value, expectedOperationId)", js)
        self.assertIn(
            'HOST_API.route("getOperation", {operation_id: operationId})',
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

    def test_host_authority_commands_fail_closed_by_authenticated_role(self):
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
            'SET_AUTHORITY: new Set(["OWNER"])',
            js,
        )
        self.assertIn(
            'role: requiredText(permissionSummary.role, "permission_summary.role")',
            js,
        )
        self.assertIn("function roleCanSubmitAction(role, action)", js)
        self.assertIn("function actionCanSubmitInCurrentScope(role, action)", js)
        self.assertIn("function syncHostActionOptions(role)", js)
        self.assertIn("option.disabled = !allowed", js)
        self.assertIn(
            "The authenticated role has no permitted host safety command. "
            "Commands remain blocked.",
            js,
        )
        self.assertIn('value="BLOCK_NEW_EXPOSURE"', html)
        self.assertIn('value="REVOKE_AUTHORITY"', html)
        self.assertIn('value="SET_AUTHORITY"', html)
        self.assertIn(
            'if (action === "SET_AUTHORITY" && state.environment === "REPLAY")',
            js,
        )


    def test_owner_authority_policy_workflow_is_structured_and_scope_bound(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        for field_id in (
            "authority-policy-fields",
            "authority-policy-host",
            "authority-policy-account",
            "authority-policy-environment",
            "authority-policy-id",
            "authority-instrument-id",
            "authority-instrument-version",
            "authority-actions",
            "authority-max-notional",
            "authority-valid-from",
            "authority-expires-at",
            "authority-policy-version",
            "authority-autonomous",
            "authority-protection-only",
            "authority-policy-confirm",
        ):
            self.assertIn(f'id="{field_id}"', html)
        self.assertNotIn("<textarea", html.lower())
        self.assertIn("function authorityPolicyPayload()", js)
        self.assertIn("environments: Object.freeze([state.environment])", js)
        self.assertIn("policy_id: requiredPolicyInput", js)
        self.assertIn("instrument_id: instrumentId.toLowerCase()", js)
        self.assertIn("actions: Object.freeze(authorityPolicyActions())", js)
        self.assertIn("max_notional: positiveDecimalPolicyInput", js)
        self.assertIn("autonomous: byId(\"authority-autonomous\").checked", js)
        self.assertIn(
            "protection_only: byId(\"authority-protection-only\").checked",
            js,
        )
        self.assertIn(
            "review confirmation is required before authority policy submission",
            js,
        )
        self.assertIn(
            'if (action === "SET_AUTHORITY") return authorityPolicyPayload()',
            js,
        )
        self.assertNotIn('name="account_id"', html)
        self.assertNotIn('name="environment"', html)


    def test_policy_actions_are_not_silently_normalized_after_review(self):
        js = APP.read_text(encoding="utf-8")
        policy = js[
            js.index("function authorityPolicyActions()"):
            js.index("function authorityPolicyPayload()")
        ]
        self.assertIn('const actions = raw.split(",")', policy)
        self.assertIn('item === "" || item !== item.trim()', policy)
        self.assertIn(
            "without surrounding whitespace or empty entries",
            policy,
        )
        self.assertNotIn(".map((item) => item.trim())", policy)
        self.assertNotIn(".filter(Boolean)", policy)

    def test_policy_form_uses_exact_integer_and_decimal_guards_before_host_submit(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function positiveSafeIntegerPolicyInput(id, name)", js)
        self.assertIn("Number.isSafeInteger(value)", js)
        self.assertIn("function positiveDecimalPolicyInput(id, name)", js)
        self.assertIn(
            'throw new Error(name + " must be a positive canonical decimal")',
            js,
        )
        self.assertIn("utcInstant(validFrom, \"valid from\")", js)
        self.assertIn("utcInstant(expiresAt, \"expires at\")", js)
        self.assertIn(
            'throw new Error("authority policy expiry must be after valid from")',
            js,
        )
        policy = js[
            js.index("function authorityPolicyPayload()"):
            js.index("function commandActionPayload")
        ]
        self.assertIn("compareCanonicalUtcInstants(", policy)
        self.assertNotIn("Date.parse(", policy)


    def test_command_submit_moves_focus_to_inflight_status_before_network_wait(self):
        js = APP.read_text(encoding="utf-8")
        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        message = submit.index('"Submitting host command " + commandId + "."')
        focus = submit.index('byId("command-result").focus();', message)
        network = submit.index("await submitCanonicalCommand(payload)", focus)
        self.assertLess(message, focus)
        self.assertLess(focus, network)

    def test_exact_authority_payload_locks_visible_policy_before_network_wait(self):
        js = APP.read_text(encoding="utf-8")
        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        payload = submit.index("payload = commandForSubmission(action)")
        lock = submit.index(
            "syncHostActionOptions(state.sessionIdentity.role)",
            payload,
        )
        restore = submit.index("renderPendingAuthorityPolicyForRetry()", lock)
        network = submit.index("await submitCanonicalCommand(payload)", restore)
        self.assertLess(payload, lock)
        self.assertLess(lock, restore)
        self.assertLess(restore, network)
        self.assertIn(
            "The operator must never see editable values",
            submit,
        )

    def test_invalid_policy_form_does_not_invalidate_fresh_host_snapshot(self):
        js = APP.read_text(encoding="utf-8")
        submit = js.index("async function submitCommand(event)")
        build = js.index("payload = commandForSubmission(action)", submit)
        local_catch = js.index("} catch (error) {", build)
        network_submit = js.index("await submitCanonicalCommand(payload)", local_catch)
        local_slice = js[local_catch:network_submit]
        self.assertIn("Command was not submitted:", local_slice)
        self.assertNotIn("state.snapshotReady = false", local_slice)
        self.assertNotIn("state.sessionIdentity = null", local_slice)


    def test_policy_review_confirmation_is_bound_to_exact_fields_and_snapshot_context(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function authorityReviewScopeKey()", js)
        self.assertIn('String(state.renderedHostId ?? "")', js)
        self.assertIn(
            'state.sessionIdentity === null ? "" : state.sessionIdentity.actor',
            js,
        )
        self.assertIn(
            'state.sessionIdentity === null ? "" : state.sessionIdentity.session',
            js,
        )
        self.assertIn("function invalidateAuthorityPolicyReview()", js)
        self.assertIn("function bindAuthorityPolicyReviewInvalidation()", js)
        self.assertIn(
            'input.addEventListener("input", invalidateAuthorityPolicyReview)',
            js,
        )
        self.assertIn(
            'input.addEventListener("change", invalidateAuthorityPolicyReview)',
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewStateVersion = state.version.toString()",
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewScope = authorityReviewScopeKey()",
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewStateVersion !== state.version.toString()",
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewScope !== authorityReviewScopeKey()",
            js,
        )
        self.assertIn(
            "review confirmation must be renewed after host state or policy scope changes",
            js,
        )
        self.assertIn(
            "priorScopeKey !== \"\" && priorScopeKey !== scopeKey",
            js,
        )
        self.assertIn("bindAuthorityPolicyReviewInvalidation();", js)


    def test_pending_authority_retry_restores_locks_and_reconfirms_exact_payload(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function renderPendingAuthorityPolicyForRetry()", js)
        self.assertIn("select.value = state.pendingCommand.action", js)
        self.assertIn("select.disabled = true", js)
        self.assertIn("input.disabled = !active || lockedForRetry", js)
        self.assertIn(
            'input.disabled = !active || (lockedForRetry && id !== "authority-policy-confirm")',
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewCommandId =",
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewCommandId !== reviewCommandId",
            js,
        )
        render = js[
            js.index("function renderPendingAuthorityPolicyForRetry()"):
            js.index("function parseCanonicalSnapshot(value)")
        ]
        self.assertIn(
            "confirmation.dataset.reviewCommandId !== state.pendingCommand.command_id",
            render,
        )
        self.assertIn(
            "confirmation.dataset.reviewStateVersion !== state.version.toString()",
            render,
        )
        self.assertIn(
            "confirmation.dataset.reviewScope !== authorityReviewScopeKey()",
            render,
        )
        self.assertIn("invalidateAuthorityPolicyReview();", render)
        self.assertIn(
            'state.pendingCommand.action === "SET_AUTHORITY"',
            js,
        )
        self.assertIn(
            "const reviewedPolicy = authorityPolicyPayload()",
            js,
        )
        self.assertIn(
            "JSON.stringify(state.pendingCommand.payload)",
            js,
        )
        self.assertIn(
            "reviewed authority policy does not exactly match the unresolved command payload",
            js,
        )
        render = js[
            js.index("function renderPendingAuthorityPolicyForRetry()"):
            js.index("function parseCanonicalSnapshot(value)")
        ]
        for field_id in (
            "authority-policy-id",
            "authority-instrument-id",
            "authority-instrument-version",
            "authority-actions",
            "authority-max-notional",
            "authority-valid-from",
            "authority-expires-at",
            "authority-policy-version",
        ):
            self.assertIn(f'byId("{field_id}").value', render)
        self.assertIn(
            'byId("authority-autonomous").checked = policy.autonomous === true',
            render,
        )
        self.assertIn(
            'byId("authority-protection-only").checked = policy.protection_only === true',
            render,
        )
        self.assertIn(
            'text("authority-policy-host", state.pendingCommandHostId)',
            render,
        )


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
            "actionCanSubmitInCurrentScope(state.sessionIdentity.role, effectiveAction)",
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
        payload = js.index("payload = commandForSubmission(action)", submit)
        role_fence = js.index(
            "if (recovering && !actionCanSubmitInCurrentScope(",
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
        self.assertIn("payload = commandForSubmission(action)", js)
        self.assertIn("clearConfirmedCommand(payload)", js)
        self.assertIn(
            "Its original command_id and idempotency_key are retained for exact retry",
            js,
        )
        submit = js.index("async function submitCommand(event)")
        construct = js.index("payload = commandForSubmission(action)", submit)
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
        self.assertIn('reapplyTableFilter("permissions-body")', js)


    def test_permission_and_operation_tables_share_keyboard_filter_copy_workflow(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        for prefix in ("permissions", "operations"):
            self.assertIn(f'id="{prefix}-filter" type="search"', html)
            self.assertIn(f'id="{prefix}-copy" type="button"', html)
            self.assertIn(f'id="{prefix}-filter-status"', html)
        self.assertIn(
            'Object.freeze({bodyId: "permissions-body", filterId: "permissions-filter"',
            js,
        )
        self.assertIn(
            'Object.freeze({bodyId: "operations-body", filterId: "operations-filter"',
            js,
        )
        render_operation = js[
            js.index("function renderOperation(operation)"):
            js.index("async function refreshOperation")
        ]
        self.assertIn('row.dataset.filterableRow = "true"', render_operation)
        self.assertIn('reapplyTableFilter("operations-body")', render_operation)

    def test_account_or_environment_scope_change_clears_operation_evidence(self):
        js = APP.read_text(encoding="utf-8")
        reset = js[
            js.index("function resetOperationsForScope("):
            js.index("function resetEventHistoryForScope(")
        ]
        self.assertIn('const body = byId("operations-body")', reset)
        self.assertIn("body.replaceChildren()", reset)
        self.assertIn("cell.colSpan = 4", reset)
        self.assertIn(
            "No host operations loaded for this account/environment session.",
            reset,
        )
        self.assertIn('reapplyTableFilter("operations-body")', reset)
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        filters = snapshot.index("resetTableFiltersForScopeChange();")
        operations = snapshot.index("resetOperationsForScope();")
        history = snapshot.index("resetEventHistoryForScope();")
        portfolio = snapshot.index('renderProjection(\n      "portfolio-body"')
        self.assertLess(filters, operations)
        self.assertLess(operations, history)
        self.assertLess(operations, portfolio)

    def test_stale_operation_failure_is_suppressed_after_scope_change(self):
        js = APP.read_text(encoding="utf-8")
        refresh = js[
            js.index("async function refreshOperation(operationId)"):
            js.index("function renderHostEvent")
        ]
        request = refresh.index("await jsonFetch(")
        catch = refresh.index("} catch (error) {", request)
        null_return = refresh.index("return null;", catch)
        rethrow = refresh.index("throw error;", null_return)
        self.assertLess(request, catch)
        self.assertLess(catch, null_return)
        self.assertLess(null_return, rethrow)
        self.assertIn("scopeEpoch !== state.scopeEpoch", refresh[catch:rethrow])
        self.assertIn(
            "renderedAccountId !== state.renderedAccountId",
            refresh[catch:rethrow],
        )
        self.assertIn(
            "renderedEnvironment !== state.renderedEnvironment",
            refresh[catch:rethrow],
        )


    def test_superseded_snapshot_failure_does_not_poison_newer_refresh(self):
        js = APP.read_text(encoding="utf-8")
        refresh = js[
            js.index("async function refreshSnapshot(options = {})"):
            js.index("function eventMessage")
        ]
        request = refresh.index("await jsonFetch(")
        catch = refresh.index("} catch (error) {", request)
        stale = refresh.index("if (refreshEpoch !== state.scopeEpoch)", catch)
        false_return = refresh.index("return false;", stale)
        rethrow = refresh.index("throw error;", false_return)
        self.assertLess(request, catch)
        self.assertLess(catch, stale)
        self.assertLess(stale, false_return)
        self.assertLess(false_return, rethrow)


    def test_event_poll_failure_cannot_invalidate_newer_scope(self):
        js = APP.read_text(encoding="utf-8")
        poll = js[
            js.index("async function pollEvents()"):
            js.index("function newCommandPayload")
        ]
        self.assertIn("let pollEpoch = null", poll)
        self.assertIn("let pollRenderedHostId = null", poll)
        self.assertIn("let pollRenderedAccountId = null", poll)
        self.assertIn("let pollRenderedEnvironment = null", poll)
        catch = poll.rindex("} catch (error) {")
        stale = poll.index("pollEpoch !== state.scopeEpoch", catch)
        early_return = poll.index("return;", stale)
        invalidation = poll.index("state.snapshotReady = false", early_return)
        self.assertLess(catch, stale)
        self.assertLess(stale, early_return)
        self.assertLess(early_return, invalidation)


    def test_event_operation_id_is_canonical_uuid_before_route_use(self):
        js = APP.read_text(encoding="utf-8")
        poll = js[
            js.index("async function pollEvents()"):
            js.index("function newCommandPayload")
        ]
        canonical = poll.index(
            'const operationId = canonicalId(\n            payload.operation_id'
        )
        refresh = poll.index("await refreshOperation(operationId)", canonical)
        self.assertLess(canonical, refresh)
        self.assertNotIn(
            'const operationId = requiredText(\n            payload.operation_id',
            poll,
        )


    def test_event_batch_must_be_canonical_json_array(self):
        js = APP.read_text(encoding="utf-8")
        poll = js[
            js.index("async function pollEvents()"):
            js.index("function newCommandPayload")
        ]
        self.assertIn("if (!Array.isArray(response))", poll)
        self.assertIn(
            "Host event response must be a canonical JSON array",
            poll,
        )
        self.assertNotIn("response.events || []", poll)


    def test_async_operation_reads_cannot_repopulate_a_changed_scope(self):
        js = APP.read_text(encoding="utf-8")
        refresh = js[
            js.index("async function refreshOperation(operationId)"):
            js.index("function renderHostEvent")
        ]
        self.assertIn("const scopeEpoch = state.scopeEpoch", refresh)
        self.assertIn("const renderedHostId = state.renderedHostId", refresh)
        self.assertIn("const renderedAccountId = state.renderedAccountId", refresh)
        self.assertIn("const renderedEnvironment = state.renderedEnvironment", refresh)
        self.assertIn("scopeEpoch !== state.scopeEpoch", refresh)
        self.assertIn("renderedHostId !== state.renderedHostId", refresh)
        self.assertIn("renderedAccountId !== state.renderedAccountId", refresh)
        self.assertIn("renderedEnvironment !== state.renderedEnvironment", refresh)
        fence = refresh.index("scopeEpoch !== state.scopeEpoch")
        render = refresh.index("renderOperation(operation)")
        self.assertLess(fence, render)
        self.assertIn("return null", refresh)

        poll = js[
            js.index("async function pollEvents()"):
            js.index("function newCommandPayload")
        ]
        self.assertIn("const operation = await refreshOperation(operationId)", poll)
        self.assertIn("if (operation === null)", poll)
        self.assertLess(
            poll.index("if (operation === null)"),
            poll.index("renderHostEvent(event, cursor, version)"),
        )

    def test_confirmed_command_response_is_scope_fenced_before_operation_rendering(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function commandContextMatchesCurrentSnapshot(payload, submittedHostId)", js)
        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        response = submit.index("const result = await submitCanonicalCommand(payload)")
        fence = submit.index("if (!responseScopeCurrent)", response)
        details = submit.index(
            'renderCommandValidationDetails(result.fieldErrors, "confirmed")',
            response,
        )
        operation = submit.index("await refreshOperation(result.operationId)", response)
        self.assertLess(fence, details)
        self.assertLess(fence, operation)
        self.assertIn(
            "Operation and field-validation details from the original scope were not rendered into the current scope.",
            submit,
        )
        self.assertIn("Acceptance is not a completed financial outcome.", submit)

    def test_trust_invalidation_advances_async_scope_epoch(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("scopeEpoch: 0", js)
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        self.assertIn("state.scopeEpoch += 1", snapshot)
        pagehide = js[
            js.index('window.addEventListener("pagehide"'):
            js.index('window.addEventListener("pageshow"')
        ]
        self.assertIn("state.scopeEpoch += 1", pagehide)

    def test_superseded_snapshot_does_not_announce_false_refresh_success(self):
        js = APP.read_text(encoding="utf-8")
        refresh_user = js[
            js.index("async function refreshStateFromUser()"):
            js.index("async function start()")
        ]
        self.assertIn("const refreshed = await refreshSnapshot()", refresh_user)
        self.assertIn("if (refreshed)", refresh_user)
        self.assertLess(
            refresh_user.index("if (refreshed)"),
            refresh_user.index("Host state refreshed from the canonical snapshot."),
        )

        pageshow = js[js.index('window.addEventListener("pageshow"'):]
        self.assertIn("const restored = await refreshSnapshot()", pageshow)
        self.assertIn("if (restored)", pageshow)
        self.assertLess(
            pageshow.index("if (restored)"),
            pageshow.index("Host state refreshed after page restoration."),
        )

        poll = js[
            js.index("async function pollEvents()"):
            js.index("function newCommandPayload")
        ]
        self.assertIn("const recovered = await refreshSnapshot()", poll)
        self.assertIn("if (!recovered)", poll)
        self.assertLess(
            poll.index("if (!recovered)"),
            poll.index("pollEpoch = state.scopeEpoch"),
        )


    def test_display_context_change_is_announced_with_host_account_and_environment(self):
        js = APP.read_text(encoding="utf-8")
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        self.assertIn("Host display context changed to host ", snapshot)
        self.assertIn('", account " + parsed.accountId', snapshot)
        self.assertIn('" in " + parsed.environment', snapshot)
        self.assertIn(
            "Old-context operation, event, notification, and command-validation evidence was cleared.",
            snapshot,
        )
        self.assertIn('parsed.environment === "LIVE" || hostChanged', snapshot)
        markers = snapshot.index("state.renderedHostId = parsed.hostId")
        announcement = snapshot.index("Host display context changed to host ")
        self.assertLess(markers, announcement)


    def test_host_identity_change_resets_local_event_context_and_counters(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("renderedHostId: null", js)
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        self.assertIn("const hostChanged =", snapshot)
        self.assertIn("parsed.hostId !== state.renderedHostId", snapshot)
        self.assertIn("const displayContextChanged = hostChanged || scopeChanged", snapshot)
        changed = snapshot.index("if (displayContextChanged)")
        cursor = snapshot.index("state.cursor = 0n", changed)
        version = snapshot.index("state.version = 0n", changed)
        operations = snapshot.index("resetOperationsForScope();", changed)
        self.assertLess(changed, cursor)
        self.assertLess(cursor, version)
        self.assertLess(version, operations)


    def test_unresolved_command_is_bound_to_exact_rendered_host_identity(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("pendingCommandHostId: null", js)
        self.assertIn(
            "state.pendingCommandHostId = state.renderedHostId",
            js,
        )
        self.assertIn(
            "state.pendingCommandHostId === state.renderedHostId",
            js,
        )
        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        self.assertIn(
            "state.pendingCommandHostId !== state.renderedHostId",
            submit,
        )
        self.assertIn("const submittedHostId = state.pendingCommandHostId", submit)
        self.assertIn(
            "commandContextMatchesCurrentSnapshot(payload, submittedHostId)",
            submit,
        )


    def test_same_scope_snapshot_cursor_jump_clears_event_derived_views(self):
        js = APP.read_text(encoding="utf-8")
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        self.assertIn("const priorCursor = state.cursor", snapshot)
        self.assertIn("const skippedSameScopeEvents =", snapshot)
        self.assertIn("parsed.cursor > priorCursor", snapshot)
        self.assertIn(
            '"Canonical snapshot advanced from event cursor " + priorCursor.toString()',
            snapshot,
        )
        self.assertIn(
            '" to " + parsed.cursor.toString()',
            snapshot,
        )
        for reset in (
            "resetNotificationsForScope(",
            "resetOperationsForScope(",
            "resetEventHistoryForScope(",
        ):
            self.assertGreaterEqual(snapshot.count(reset), 2)
        self.assertIn(
            "Event-derived operation, notification, and received-event views were cleared rather than shown as current.",
            snapshot,
        )
        gap = snapshot.index("if (skippedSameScopeEvents)")
        cursor_commit = snapshot.index("state.cursor = parsed.cursor")
        self.assertLess(gap, cursor_commit)
        self.assertIn("true);", snapshot[gap:cursor_commit])

    def test_same_scope_snapshot_without_cursor_jump_preserves_event_derived_views(self):
        js = APP.read_text(encoding="utf-8")
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        condition = snapshot[
            snapshot.index("const skippedSameScopeEvents ="):
            snapshot.index("if (displayContextChanged)")
        ]
        self.assertIn("!displayContextChanged", condition)
        self.assertIn("state.renderedHostId !== null", condition)
        self.assertIn("parsed.cursor > priorCursor", condition)
        self.assertNotIn("parsed.cursor >= priorCursor", condition)

    def test_snapshot_and_event_stream_responses_are_generation_fenced(self):
        js = APP.read_text(encoding="utf-8")
        refresh = js[
            js.index("async function refreshSnapshot(options = {})"):
            js.index("function eventMessage")
        ]
        self.assertIn("state.scopeEpoch += 1", refresh)
        self.assertIn("const refreshEpoch = state.scopeEpoch", refresh)
        self.assertIn("if (refreshEpoch !== state.scopeEpoch)", refresh)
        self.assertLess(
            refresh.index("if (refreshEpoch !== state.scopeEpoch)"),
            refresh.index("renderSnapshot(snapshot, options)"),
        )

        poll = js[
            js.index("async function pollEvents()"):
            js.index("function newCommandPayload")
        ]
        self.assertIn("pollEpoch = state.scopeEpoch", poll)
        self.assertIn("pollRenderedAccountId = state.renderedAccountId", poll)
        self.assertIn("pollRenderedEnvironment = state.renderedEnvironment", poll)
        self.assertIn("pollEpoch !== state.scopeEpoch", poll)
        response = poll.index("const response = await jsonFetch(")
        fence = poll.index("pollEpoch !== state.scopeEpoch", response)
        event_render = poll.index("renderHostEvent(event, cursor, version)", fence)
        self.assertLess(response, fence)
        self.assertLess(fence, event_render)

    def test_old_command_failure_cannot_invalidate_new_scope(self):
        js = APP.read_text(encoding="utf-8")
        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        catch_start = submit.rindex("    } catch {")
        catch_block = submit[catch_start:]
        self.assertIn("if (commandContextMatchesCurrentSnapshot(payload, submittedHostId))", catch_block)
        self.assertIn("state.snapshotReady = false", catch_block)
        self.assertIn(
            "The current scope snapshot is not invalidated by this older request.",
            catch_block,
        )
        self.assertIn(
            "Its original command_id and idempotency_key are retained and will not be retargeted.",
            catch_block,
        )

    def test_accepted_operation_scope_change_stops_old_submit_flow(self):
        js = APP.read_text(encoding="utf-8")
        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        null_fence = submit.index("if (operation === null)")
        post_refresh = submit.index("await refreshSnapshot();", null_fence)
        self.assertIn("This accepted response belongs to the original scope", submit)
        self.assertIn('byId("command-result").focus();', submit[null_fence:post_refresh])
        self.assertIn("return;", submit[null_fence:post_refresh])

    def test_clipboard_completion_feedback_is_scope_fenced(self):
        js = APP.read_text(encoding="utf-8")
        copy = js[
            js.index("async function copyVisibleTableRows(tool)"):
            js.index("function bindTableTools()")
        ]
        self.assertIn("const scopeEpoch = state.scopeEpoch", copy)
        self.assertGreaterEqual(copy.count("if (scopeEpoch !== state.scopeEpoch)"), 2)
        write = copy.index("await navigator.clipboard.writeText(payload)")
        success_fence = copy.index("if (scopeEpoch !== state.scopeEpoch)", write)
        success_message = copy.index('" visible " + tool.label + " rows copied."', success_fence)
        self.assertLess(write, success_fence)
        self.assertLess(success_fence, success_message)

    def test_scope_change_clears_unscoped_command_and_notification_feedback(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("function resetNotificationsForScope(", js)
        self.assertIn("window.clearTimeout(state.announcementTimer)", js)
        self.assertIn("window.clearTimeout(state.urgentAnnouncementTimer)", js)
        self.assertIn("state.pendingAnnouncements = []", js)
        self.assertIn("state.pendingUrgentAnnouncements = []", js)
        self.assertIn(
            "No material notifications recorded in this account/environment session.",
            js,
        )
        self.assertIn(
            "function resetCommandFeedbackForContext(",
            js,
        )
        self.assertIn(
            "An unresolved command from a different host/session/account/environment context is retained with its original identity and will not be retargeted.",
            js,
        )
        self.assertIn(
            "No host command has been submitted for this host/account/environment session.",
            js,
        )
        self.assertIn('renderCommandValidationDetails([], "scope_changed")', js)
        self.assertIn(
            "Field-validation details from the previous account/environment scope are not shown in this scope.",
            js,
        )

        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        notifications = snapshot.index("resetNotificationsForScope();")
        filters = snapshot.index("resetTableFiltersForScopeChange();")
        operations = snapshot.index("resetOperationsForScope();")
        history = snapshot.index("resetEventHistoryForScope();")
        command = snapshot.index(
            "resetCommandFeedbackForContext(\n        parsed.hostId,\n        parsed.accountId,\n        parsed.environment,\n        parsed.sessionIdentity);"
        )
        self.assertLess(notifications, filters)
        self.assertLess(filters, operations)
        self.assertLess(operations, history)
        self.assertLess(history, command)


    def test_live_projection_tables_have_keyboard_filter_copy_sort_and_page_controls(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        for prefix in ("permissions", "strategy", "portfolio", "operations", "risk", "jobs", "event-history"):
            self.assertIn(f'id="{prefix}-filter" type="search"', html)
            self.assertIn(f'id="{prefix}-copy" type="button"', html)
            self.assertIn(f'id="{prefix}-sort"', html)
            self.assertIn(f'id="{prefix}-previous" type="button"', html)
            self.assertIn(f'id="{prefix}-next" type="button"', html)
            self.assertIn(f'id="{prefix}-filter-status"', html)
        self.assertIn("const TABLE_PAGE_SIZE = 25", js)
        self.assertIn("const tableViewState = new Map()", js)
        self.assertIn("function applyTableFilter(tool, {announce = true, resetPage = false} = {})", js)
        self.assertIn("function orderedTableRows(tool, rows, mode)", js)
        self.assertIn("function copyVisibleTableRows(tool)", js)
        self.assertIn("function bindTableTools()", js)
        self.assertIn("bindTableTools();", js)

    def test_projection_filter_is_local_text_only_and_reapplied_after_live_updates(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn('row.dataset.filterableRow = "true"', js)
        self.assertIn("tableSearchText(row).includes(query)", js)
        self.assertIn("row.hidden = !matches", js)
        self.assertIn('reapplyTableFilter("jobs-body")', js)
        self.assertIn('reapplyTableFilter("event-history-body")', js)
        self.assertIn("reapplyTableFilter(bodyId)", js)
        scope = js[js.index("function applyTableFilter"):js.index("function reapplyTableFilter")]
        self.assertNotIn("fetch(", scope)
        self.assertNotIn("submitCanonicalCommand", scope)
        self.assertNotIn("innerHTML", scope)

    def test_projection_filter_case_normalization_is_locale_deterministic(self):
        js = APP.read_text(encoding="utf-8")
        scope = js[js.index("function normalizedTableQuery"):js.index("function reapplyTableFilter")]
        self.assertIn("toLowerCase()", scope)
        self.assertNotIn("toLocaleLowerCase()", scope)

    def test_passive_live_refresh_updates_filter_status_without_announcing(self):
        js = APP.read_text(encoding="utf-8")
        apply_scope = js[js.index("function applyTableFilter"):js.index("function reapplyTableFilter")]
        self.assertIn("text(tool.statusId, statusMessage);", apply_scope)
        self.assertIn("if (announce) queuePoliteAnnouncement(statusMessage);", apply_scope)
        reapply = js[js.index("function reapplyTableFilter"):js.index("function resetTableFiltersForScopeChange")]
        self.assertIn("applyTableFilter(tool, {announce: false})", reapply)

    def test_scope_transition_clears_filters_before_new_scope_render_and_announces_once(self):
        js = APP.read_text(encoding="utf-8")
        reset = js[js.index("function resetTableFiltersForScopeChange"):js.index("function visibleTableRows")]
        self.assertIn("for (const tool of TABLE_TOOLS)", reset)
        self.assertIn('filter.value = ""', reset)
        self.assertEqual(reset.count("queuePoliteAnnouncement("), 1)
        snapshot = js[js.index("function renderSnapshot(snapshot"):js.index("async function refreshSnapshot")]
        self.assertLess(snapshot.index("resetTableFiltersForScopeChange();"), snapshot.index("resetEventHistoryForScope();"))
        self.assertLess(snapshot.index("resetTableFiltersForScopeChange();"), snapshot.index('renderProjection(\n      "portfolio-body"'))

    def test_copy_visible_rows_uses_only_rendered_text_and_accessible_fallback(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("filterableRows(body).filter((row) => !row.hidden)", js)
        self.assertIn('cell.textContent.replace(/\\s+/g, " ").trim()', js)
        self.assertIn("await navigator.clipboard.writeText(payload)", js)
        self.assertIn("Clipboard access is unavailable. Use normal text selection and copy.", js)
        self.assertIn("Clipboard copy was not permitted. Use normal text selection and copy.", js)
        self.assertNotIn("document.execCommand", js)

    def test_table_tools_use_one_polite_live_region_not_five_status_live_regions(self):
        html = INDEX.read_text(encoding="utf-8")
        parser = _ElementParser()
        parser.feed(html)
        by_id = {attrs["id"]: (tag, attrs) for tag, attrs in parser.elements if "id" in attrs}
        for prefix in ("permissions", "strategy", "portfolio", "operations", "risk", "jobs", "event-history"):
            status_id = f"{prefix}-filter-status"
            tag, attrs = by_id[status_id]
            self.assertEqual(tag, "p", status_id)
            self.assertNotIn("role", attrs, status_id)
            self.assertNotIn("aria-live", attrs, status_id)
            tag, attrs = by_id[f"{prefix}-filter"]
            self.assertEqual(tag, "input", prefix)
            self.assertIn(status_id, attrs.get("aria-describedby", "").split())
        # Native output is implicitly a polite status region even without ARIA.
        # Snapshot metadata must remain readable without announcing every poll.
        live_ids = [
            attrs.get("id") for tag, attrs in parser.elements
            if tag == "output" or attrs.get("role") in {"status", "alert", "log"}
            or attrs.get("aria-live", "off") != "off"
        ]
        self.assertEqual(
            live_ids, ["polite-status", "urgent-status"],
        )

    def test_table_tool_focus_targets_survive_browser_page_restore(self):
        js = APP.read_text(encoding="utf-8")
        for target in (
            "permissions-filter", "permissions-copy",
            "strategy-filter", "strategy-copy", "portfolio-filter", "portfolio-copy",
            "operations-filter", "operations-copy",
            "risk-filter", "risk-copy", "jobs-filter", "jobs-copy",
            "event-history-filter", "event-history-copy",
        ):
            self.assertIn(f'"{target}"', js)

    def test_table_sort_pagination_event_order_and_clipboard_headers_are_deterministic(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("Math.ceil(matching.length / TABLE_PAGE_SIZE)", js)
        self.assertIn('tool.bodyId === "event-history-body"', js)
        self.assertIn("BigInt(left.dataset.tableHostOrder)", js)
        self.assertIn("BigInt(right.dataset.tableHostOrder)", js)
        self.assertIn("function tabSeparatedTableHeaderText(tool)", js)
        self.assertIn('table.querySelectorAll("thead th")', js)
        self.assertIn("[header, ...rowPayload]", js)
        self.assertIn("Column headings included.", js)
        for prefix in ("permissions", "strategy", "portfolio", "operations", "risk", "jobs", "event-history"):
            for suffix in ("sort", "previous", "next"):
                self.assertIn(f'"{prefix}-{suffix}"', js)

    def test_table_tools_reflow_without_horizontal_viewport_locking(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".table-tools {", css)
        self.assertIn("flex-wrap: wrap", css)
        self.assertIn("max-inline-size: 100%", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertNotIn("overflow-x: hidden", css.lower())

    def test_table_ui_feedback_uses_speech_queue_not_material_notification_history(self):
        js = APP.read_text(encoding="utf-8")
        queue = js[js.index("function queuePoliteAnnouncement"):js.index("function announce(message")]
        self.assertIn("state.pendingAnnouncements.push(message)", queue)
        self.assertIn('announceLiveText("polite-status", pending.join(" "))', queue)
        self.assertNotIn("notification-history", queue)
        copy = js[js.index("async function copyVisibleTableRows"):js.index("function bindTableTools")]
        self.assertNotIn("announce(message", copy)


    def test_set_authority_preserves_generated_routes_and_canonical_operation_ids(self):
        js = APP.read_text(encoding="utf-8")
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn("const HOST_API = window.AutoTradeHostApi", js)
        self.assertNotIn('const API = "/api/v1"', js)
        self.assertIn('HOST_API.route("submitCommand")', js)
        self.assertIn(
            'HOST_API.route("getOperation", {operation_id: operationId})',
            js,
        )
        self.assertIn(
            'const operationId = canonicalId(result.operation_id, "operation_id")',
            js,
        )
        self.assertIn(
            'const commandId = canonicalId(result.command_id, "command_id")',
            js,
        )
        self.assertEqual(js.count("async function submitCanonicalCommand(payload)"), 1)
        routes = html.index('<script src="/host-api-routes.js" defer></script>')
        app = html.index('<script src="/app.js" defer></script>')
        self.assertLess(routes, app)

    def test_authority_review_key_and_visible_scope_include_exact_host_identity(self):
        html = INDEX.read_text(encoding="utf-8")
        js = APP.read_text(encoding="utf-8")
        self.assertIn('id="authority-policy-host"', html)
        self.assertIn(
            "I reviewed the exact host, account, environment, instrument, actions, notional and validity window above.",
            html,
        )
        review = js[
            js.index("function authorityReviewScopeKey()"):
            js.index("function invalidateAuthorityPolicyReview()")
        ]
        self.assertIn('String(state.renderedHostId ?? "")', review)
        self.assertIn('text("authority-policy-host", active ? state.renderedHostId : null)', js)

    def test_host_identity_change_invalidates_authority_review_and_retry_context(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("renderedHostId: null", js)
        self.assertIn("pendingCommandHostId: null", js)
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        self.assertIn("const hostChanged =", snapshot)
        self.assertIn("const displayContextChanged = hostChanged || scopeChanged", snapshot)
        self.assertIn("if (displayContextChanged)", snapshot)
        self.assertIn("state.renderedHostId = parsed.hostId", snapshot)
        self.assertIn('parsed.environment === "LIVE" || hostChanged', snapshot)
        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        self.assertIn("state.pendingCommandHostId !== state.renderedHostId", submit)
        self.assertIn("const submittedHostId = state.pendingCommandHostId", submit)
        self.assertIn(
            "commandContextMatchesCurrentSnapshot(payload, submittedHostId)",
            submit,
        )

    def test_set_authority_payload_matches_current_host_validator_shape(self):
        js = APP.read_text(encoding="utf-8")
        host = (ROOT / "mvp" / "autotrade_mvp" / "operator_authority_commands.py").read_text(
            encoding="utf-8"
        )
        for field in (
            "policy_id",
            "environments",
            "instruments",
            "actions",
            "max_notional",
            "expires_at",
            "autonomous",
            "valid_from",
            "protection_only",
            "version",
        ):
            self.assertIn(field + ":", js)
            self.assertIn(f'"{field}"', host)
        self.assertIn('if action_name == "SET_AUTHORITY":', host)
        self.assertIn('policy.environments != frozenset({env})', host)
        self.assertIn('environments: Object.freeze([state.environment])', js)
        self.assertIn('if (state.environment === "REPLAY")', js)

    def test_authority_review_reuses_current_scope_and_cursor_gap_fences(self):
        js = APP.read_text(encoding="utf-8")
        self.assertIn("scopeEpoch: 0", js)
        self.assertIn("function pendingCommandMatchesCurrentContext()", js)
        self.assertIn("const pendingContextMatches = pendingCommandMatchesCurrentContext()", js)
        self.assertIn(
            "button.disabled = !enabled || !roleAllowed || !pendingContextMatches",
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewStateVersion !== state.version.toString()",
            js,
        )
        self.assertIn(
            "confirmation.dataset.reviewScope !== authorityReviewScopeKey()",
            js,
        )
        self.assertIn("state.pendingCommand.account_id !== state.accountId", js)
        self.assertIn("state.pendingCommand.environment !== state.environment", js)
        self.assertIn("const skippedSameScopeEvents =", js)
        snapshot = js[
            js.index("function renderSnapshot(snapshot"):
            js.index("async function refreshSnapshot")
        ]
        gap = snapshot.index("if (skippedSameScopeEvents)")
        state_version = snapshot.index("state.version = parsed.version")
        policy_sync = snapshot.index("renderPendingAuthorityPolicyForRetry()")
        self.assertLess(gap, state_version)
        self.assertLess(state_version, policy_sync)

    def test_authority_policy_focus_targets_survive_page_restore(self):
        js = APP.read_text(encoding="utf-8")
        for target in (
            "authority-policy-id",
            "authority-instrument-id",
            "authority-instrument-version",
            "authority-actions",
            "authority-max-notional",
            "authority-valid-from",
            "authority-expires-at",
            "authority-policy-version",
            "authority-policy-confirm",
        ):
            self.assertIn(f'"{target}"', js)



    def test_snapshot_busy_is_structured_retryable_and_accessibly_fail_closed(self):
        js = APP.read_text(encoding="utf-8")
        fetch = js[js.index("async function jsonFetch"):js.index("function renderOperation")]
        self.assertIn('const contentType = response.headers.get("Content-Type") || ""', fetch)
        self.assertIn("errorBody = await response.json()", fetch)
        self.assertIn("error.code = errorBody.error", fetch)
        self.assertIn("error.retryable = true", fetch)

        classifier = js[js.index("function isSnapshotBusy"):js.index("function reportSnapshotBusy")]
        self.assertIn('error.status === 503', classifier)
        self.assertIn('error.code === "SNAPSHOT_BUSY"', classifier)
        self.assertIn("error.retryable === true", classifier)

        busy = js[js.index("function reportSnapshotBusy"):js.index("async function jsonFetch")]
        self.assertIn("invalidateSnapshotAuthority();", busy)
        self.assertIn("Commands remain blocked", busy)
        self.assertIn("waiting for one coherent snapshot", busy)
        self.assertIn("queuePoliteAnnouncement(message)", busy)
        self.assertNotIn("announce(message", busy)

        poll = js[js.index("async function pollEvents()"):js.index("function newCommandPayload")]
        self.assertIn("if (isSnapshotBusy(error))", poll)
        self.assertIn("reportSnapshotBusy();", poll)
        self.assertLess(
            poll.index("if (isSnapshotBusy(error))"),
            poll.index("Host synchronization failed. Displayed values may be stale."),
        )

        refresh = js[js.index("async function refreshStateFromUser"):js.index("async function start")]
        self.assertIn("if (isSnapshotBusy(error))", refresh)
        self.assertIn("reportSnapshotBusy();", refresh)

    def test_refresh_reveals_same_page_for_still_matching_selected_evidence(self):
        js = APP.read_text(encoding="utf-8")
        preserve = js[js.index("function preserveTableSelection"):js.index("function appendProjectionRow")]
        self.assertIn("revealBookmarkedTablePage(body, bookmark);", preserve)
        reveal = js[js.index("function revealBookmarkedTablePage"):js.index("function restoreTableSelection")]
        self.assertIn("const anchorRow = rowForSelectionEndpoint(body, bookmark.anchor);", reveal)
        self.assertIn("const focusRow = rowForSelectionEndpoint(body, bookmark.focus);", reveal)
        self.assertIn("const anchorIndex = matching.indexOf(anchorRow);", reveal)
        self.assertIn("const focusIndex = matching.indexOf(focusRow);", reveal)
        self.assertIn("if (anchorIndex < 0 || focusIndex < 0) return;", reveal)
        self.assertIn("const anchorPage = Math.floor(anchorIndex / TABLE_PAGE_SIZE);", reveal)
        self.assertIn("const focusPage = Math.floor(focusIndex / TABLE_PAGE_SIZE);", reveal)
        self.assertIn("if (anchorPage !== focusPage) return;", reveal)
        self.assertIn("view.page = anchorPage;", reveal)
        self.assertIn("applyTableFilter(tool, {announce: false});", reveal)

    def test_live_event_history_selection_has_stable_identity_and_page_reveal(self):
        js = APP.read_text(encoding="utf-8")
        render = js[js.index("function renderHostEvent"):js.index("function resetEventHistoryForScope")]
        self.assertIn("const bookmark = captureTableSelection(body);", render)
        self.assertIn('row.dataset.selectionKey = "event:" + cursor.toString();', render)
        self.assertIn('row.dataset.selectionExact = "true";', render)
        self.assertIn('reapplyTableFilter("event-history-body");', render)
        self.assertLess(
            render.index('reapplyTableFilter("event-history-body");'),
            render.index("revealBookmarkedTablePage(body, bookmark);"),
        )
        self.assertLess(
            render.index("revealBookmarkedTablePage(body, bookmark);"),
            render.index("restoreTableSelection(body, bookmark);"),
        )

    def test_selection_restore_reveals_the_bookmarked_page_before_retargeting(self):
        js = APP.read_text(encoding="utf-8")
        reveal = js[js.index("function revealBookmarkedTablePage"):js.index("function restoreTableSelection")]
        self.assertIn("const anchorIndex = matching.indexOf(anchorRow);", reveal)
        self.assertIn("const focusIndex = matching.indexOf(focusRow);", reveal)
        self.assertIn("const anchorPage = Math.floor(anchorIndex / TABLE_PAGE_SIZE);", reveal)
        self.assertIn("if (anchorPage !== focusPage) return;", reveal)
        self.assertIn("view.page = anchorPage;", reveal)
        self.assertIn("applyTableFilter(tool, {announce: false});", reveal)
        preserve = js[js.index("function preserveTableSelection"):js.index("function appendProjectionRow")]
        self.assertLess(
            preserve.index("revealBookmarkedTablePage(body, bookmark);"),
            preserve.index("restoreTableSelection(body, bookmark);"),
        )



    def test_event_history_retention_is_cursor_based_not_dom_sort_based(self):
        js = APP.read_text(encoding="utf-8")
        render = js[js.index("function renderHostEvent"):js.index("function resetEventHistoryForScope")]
        self.assertIn("const retained = filterableRows(body)", render)
        self.assertIn("BigInt(left.dataset.hostEventCursor)", render)
        self.assertIn("BigInt(right.dataset.hostEventCursor)", render)
        self.assertIn("for (const expired of retained.slice(100)) expired.remove();", render)
        self.assertNotIn("body.lastElementChild.remove()", render)

    def test_fresh_auth_rejection_discards_only_definitively_unaccepted_identity(self):
        js = APP.read_text(encoding="utf-8")
        classifier = js[
            js.index("function isCommandAuthRejection"):
            js.index("function reportSnapshotBusy")
        ]
        self.assertIn("error.status === 403", classifier)
        self.assertIn(
            'error.code === "AUTHENTICATION_OR_AUTHORIZATION_FAILED"',
            classifier,
        )
        self.assertNotIn("error.status === 401", classifier)

        transport = js[
            js.index("async function submitCanonicalCommand"):
            js.index("function text(")
        ]
        self.assertIn('contentType.includes("application/json")', transport)
        self.assertIn("errorBody = await response.json()", transport)
        self.assertIn("error.code = errorBody.error", transport)

        submit = js[
            js.index("async function submitCommand(event)"):
            js.index("async function refreshStateFromUser")
        ]
        catch = submit.index("} catch (error) {")
        definitive = submit.index(
            "if (!recovering && isCommandAuthRejection(error))",
            catch,
        )
        clear = submit.index("clearConfirmedCommand(payload)", definitive)
        ambiguous = submit.index(
            "could not be confirmed. Its original command_id and idempotency_key "
            "are retained for exact retry",
            definitive,
        )
        self.assertLess(definitive, clear)
        self.assertLess(clear, ambiguous)
        self.assertIn(
            "was not accepted because the authenticated host session was rejected "
            "before command acceptance",
            submit[definitive:ambiguous],
        )
        self.assertIn(
            "A retry is different: its prior attempt may already be durable",
            submit[definitive:ambiguous],
        )


    def test_browser_script_has_one_canonical_render_and_transport_authority(self):
        js = APP.read_text(encoding="utf-8")
        self.assertNotIn("async async function", js)
        self.assertEqual(js.count("async function jsonFetch("), 1)
        self.assertEqual(js.count("function text(id, value"), 1)
        self.assertEqual(js.count("function stableProjectionValue("), 1)
        self.assertEqual(js.count("function projectionText("), 1)
        self.assertEqual(js.count("function resetOperationsForScope"), 1)

        projection = js[
            js.index("function appendProjectionRow"):
            js.index("function renderProjection")
        ]
        self.assertIn('row.dataset.selectionKey = "projection:" + label', projection)
        self.assertIn("return row;", projection)

        operation = js[
            js.index("function renderOperation"):
            js.index("async function refreshOperation")
        ]
        self.assertIn("const bookmark = captureTableSelection(body);", operation)
        self.assertIn(
            'row.dataset.selectionKey = "operation:" + operation.operationId',
            operation,
        )
        self.assertIn('row.dataset.selectionExact = "true"', operation)
        self.assertIn("revealBookmarkedTablePage(body, bookmark);", operation)
        self.assertIn("restoreTableSelection(body, bookmark);", operation)

if __name__ == "__main__":
    unittest.main()
