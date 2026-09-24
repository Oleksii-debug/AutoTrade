(() => {
  "use strict";

  const API = "/api/v1";
  const MATERIAL_EVENTS = new Set([
    "COMMAND_ACCEPTED",
    "OPERATION_UPDATED",
    "RISK_CHANGED",
    "AUTHORITY_CHANGED",
    "AUTHORITY_REVOKED",
    "FILL_RECORDED",
    "PROVIDER_DEGRADED",
    "PROVIDER_RECOVERED",
    "RECONCILIATION_REQUIRED",
    "RECONCILIATION_COMPLETED"
  ]);
  const URGENT_EVENTS = new Set([
    "PROVIDER_DEGRADED",
    "RECONCILIATION_REQUIRED",
    "AUTHORITY_REVOKED"
  ]);

  const state = {
    cursor: 0n,
    version: 0n,
    sessionIdentity: null,
    snapshotReady: false,
    polling: false,
    stopped: false,
    announcementTimer: null,
    pendingAnnouncements: []
  };

  const byId = (id) => document.getElementById(id);

  function exactCounter(value, name) {
    if (typeof value === "bigint") {
      if (value < 0n) throw new Error(name + " must be non-negative");
      return value;
    }
    if (typeof value === "number") {
      if (!Number.isSafeInteger(value) || value < 0) {
        throw new Error(name + " must be an exact non-negative integer");
      }
      return BigInt(value);
    }
    if (value === null || value === undefined) {
      throw new Error(name + " is required");
    }
    const token = String(value);
    if (!/^(0|[1-9][0-9]*)$/.test(token)) {
      throw new Error(name + " must be a canonical non-negative integer");
    }
    return BigInt(token);
  }

  function requiredText(value, name) {
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error(name + " must be a non-empty string");
    }
    return value;
  }

  function requiredObject(value, name) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(name + " must be an object");
    }
    return value;
  }

  function requiredStringArray(value, name) {
    if (!Array.isArray(value) ||
        value.some((item) => typeof item !== "string" || item.length === 0)) {
      throw new Error(name + " must be an array of non-empty strings");
    }
    return value;
  }

  function readSessionIdentity(permissionSummary) {
    const actor = permissionSummary.actor;
    const session = permissionSummary.session;
    if (actor === undefined && session === undefined) return null;
    return {
      actor: requiredText(actor, "permission_summary.actor"),
      session: requiredText(session, "permission_summary.session")
    };
  }

  function parseCanonicalSnapshot(value) {
    const snapshot = requiredObject(value, "UiSnapshot");
    const allowed = new Set([
      "state_version", "event_cursor", "server_time", "host_id", "account_id",
      "environment", "permission_summary", "connection_freshness", "portfolio",
      "risk", "strategy", "jobs", "reason_codes"
    ]);
    for (const key of Object.keys(snapshot)) {
      if (!allowed.has(key)) throw new Error("UiSnapshot contains non-canonical field " + key);
    }

    const connectionFreshness = requiredObject(
      snapshot.connection_freshness, "connection_freshness");
    if (Object.keys(connectionFreshness).length === 0) {
      throw new Error("connection_freshness evidence is required");
    }
    const permissionSummary = requiredObject(
      snapshot.permission_summary, "permission_summary");
    if (!Array.isArray(snapshot.jobs)) throw new Error("jobs must be an array");

    return {
      version: exactCounter(snapshot.state_version, "state_version"),
      cursor: exactCounter(snapshot.event_cursor, "event_cursor"),
      serverTime: requiredText(snapshot.server_time, "server_time"),
      hostId: requiredText(snapshot.host_id, "host_id"),
      accountId: requiredText(snapshot.account_id, "account_id"),
      environment: requiredText(snapshot.environment, "environment"),
      permissionSummary,
      connectionFreshness,
      portfolio: requiredObject(snapshot.portfolio, "portfolio"),
      risk: requiredObject(snapshot.risk, "risk"),
      strategy: requiredObject(snapshot.strategy, "strategy"),
      jobs: snapshot.jobs,
      reasonCodes: requiredStringArray(snapshot.reason_codes, "reason_codes"),
      sessionIdentity: readSessionIdentity(permissionSummary)
    };
  }

  function parseCommandResult(value, expectedCommandId) {
    const result = requiredObject(value, "CommandResult");
    const allowed = new Set([
      "command_id", "operation_id", "status", "state_version",
      "reason_codes", "field_errors", "current_value_ref"
    ]);
    for (const key of Object.keys(result)) {
      if (!allowed.has(key)) throw new Error("CommandResult contains non-canonical field " + key);
    }
    const commandId = requiredText(result.command_id, "command_id");
    if (commandId !== expectedCommandId) {
      throw new Error("CommandResult command_id does not match the submitted command");
    }
    if (!["ACCEPTED", "REJECTED", "CONFLICT"].includes(result.status)) {
      throw new Error("CommandResult status is not canonical");
    }
    if (!Array.isArray(result.field_errors)) {
      throw new Error("field_errors must be an array");
    }
    return {
      commandId,
      operationId: result.operation_id === undefined
        ? null
        : requiredText(result.operation_id, "operation_id"),
      status: result.status,
      stateVersion: exactCounter(result.state_version, "CommandResult.state_version"),
      reasonCodes: requiredStringArray(result.reason_codes, "CommandResult.reason_codes"),
      fieldErrors: result.field_errors
    };
  }

  function setCommandAvailability(enabled) {
    const form = byId("host-command-form");
    const button = form && form.querySelector('button[type="submit"]');
    if (button) button.disabled = !enabled;
  }

  function freshnessText(parsed) {
    const details = Object.entries(parsed.connectionFreshness)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, value]) => key + "=" +
        (value && typeof value === "object" ? JSON.stringify(value) : String(value)))
      .join("; ");
    const reasons = parsed.reasonCodes.length > 0
      ? " Reason codes: " + parsed.reasonCodes.join(", ") + "."
      : " No host reason codes.";
    return "Host-provided freshness evidence at " + parsed.serverTime + ": " +
      details + "." + reasons;
  }

  async function submitCanonicalCommand(payload) {
    const response = await fetch(API + "/commands", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload)
    });
    if (response.status !== 200 && response.status !== 409) {
      const error = new Error("Host command failed with status " + response.status);
      error.status = response.status;
      throw error;
    }
    return parseCommandResult(await response.json(), payload.command_id);
  }

  function text(id, value, fallback = "Unavailable") {
    const element = byId(id);
    if (!element) return;
    const rendered = value === null || value === undefined || value === ""
      ? fallback
      : String(value);
    if (element.textContent !== rendered) {
      element.textContent = rendered;
    }
  }

  function announce(message, urgent = false) {
    if (!message) return;
    const history = byId("notification-history");
    if (history && history.children.length === 1 &&
        history.firstElementChild.textContent.startsWith("No material")) {
      history.replaceChildren();
    }
    if (history) {
      const item = document.createElement("li");
      item.textContent = message;
      history.prepend(item);
      while (history.children.length > 50) {
        history.lastElementChild.remove();
      }
    }

    if (urgent) {
      text("urgent-status", message, "");
      return;
    }

    state.pendingAnnouncements.push(message);
    if (state.announcementTimer !== null) return;
    state.announcementTimer = window.setTimeout(() => {
      const unique = [...new Set(state.pendingAnnouncements)];
      state.pendingAnnouncements = [];
      state.announcementTimer = null;
      text("polite-status", unique.join(" "), "");
    }, 750);
  }

  async function jsonFetch(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Accept": "application/json",
        ...(options.body ? {"Content-Type": "application/json"} : {}),
        ...(options.headers || {})
      },
      ...options
    });
    if (!response.ok) {
      const error = new Error(`Host request failed with status ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function renderSnapshot(snapshot, {announceRefresh = false} = {}) {
    const parsed = parseCanonicalSnapshot(snapshot);
    if (parsed.version < state.version || parsed.cursor < state.cursor) {
      throw new Error("host snapshot counters regressed");
    }

    state.version = parsed.version;
    state.cursor = parsed.cursor;
    state.sessionIdentity = parsed.sessionIdentity;
    state.snapshotReady = true;

    text("state-version", state.version.toString());
    text("event-cursor", state.cursor.toString());
    const expected = byId("expected-state-version");
    if (expected) expected.value = state.version.toString();

    text("active-host", parsed.hostId);
    text("active-account", parsed.accountId);
    text("active-environment", parsed.environment);
    text(
      "connection-summary",
      "Host: " + parsed.hostId + ". Account: " + parsed.accountId +
        ". Environment: " + parsed.environment + ".");
    text("freshness", freshnessText(parsed));

    const canSubmit = parsed.sessionIdentity !== null;
    setCommandAvailability(canSubmit);
    if (!canSubmit) {
      text(
        "command-result",
        "Authenticated host session identity is unavailable. Commands remain blocked.");
    }

    if (announceRefresh) {
      announce("Host state snapshot refreshed after an event cursor gap.");
    }
  }

  async function refreshSnapshot(options = {}) {
    const snapshot = await jsonFetch(`${API}/state`);
    renderSnapshot(snapshot, options);
  }

  function eventMessage(event) {
    const kind = String(event.kind ?? event.event_type ?? "EVENT");
    const payload = event.payload && typeof event.payload === "object"
      ? event.payload
      : {};
    if (kind === "OPERATION_UPDATED") {
      return `Operation ${payload.operation_id ?? "unknown"} is now ${payload.phase ?? "unknown"}.`;
    }
    if (kind === "COMMAND_ACCEPTED") {
      return `Command ${payload.command_id ?? "unknown"} was accepted. This is not a financial completion.`;
    }
    if (kind === "FILL_RECORDED") {
      return `A provider-evidenced fill was recorded for ${payload.instrument_version ?? "an instrument"}.`;
    }
    if (kind === "AUTHORITY_REVOKED") {
      return "Trading authority was revoked. New actions requiring that authority must remain blocked.";
    }
    if (kind === "PROVIDER_DEGRADED") {
      return "Provider connectivity or authentication is degraded. New risk may be blocked.";
    }
    if (kind === "RECONCILIATION_REQUIRED") {
      return "Reconciliation is required before affected new risk can be trusted.";
    }
    return kind.replaceAll("_", " ").toLowerCase() + ".";
  }

  async function pollEvents() {
    if (state.polling || state.stopped) return;
    state.polling = true;
    try {
      const response = await jsonFetch(
        `${API}/events?after=${encodeURIComponent(state.cursor.toString())}`);
      const events = Array.isArray(response) ? response : (response.events || []);
      let expectedCursor = state.cursor + 1n;
      for (const event of events) {
        const cursor = exactCounter(event.cursor, "event.cursor");
        if (cursor !== expectedCursor) {
          await refreshSnapshot({announceRefresh: true});
          return;
        }
        state.cursor = cursor;
        expectedCursor = cursor + 1n;
        const version = exactCounter(event.state_version, "event.state_version");
        if (version < state.version) {
          await refreshSnapshot({announceRefresh: true});
          return;
        }
        if (version > state.version) {
          state.version = version;
        }
        const kind = String(event.kind ?? event.event_type ?? "");
        if (MATERIAL_EVENTS.has(kind)) {
          announce(eventMessage(event), URGENT_EVENTS.has(kind));
        }
      }
      text("event-cursor", state.cursor.toString());
      text("state-version", state.version.toString());
      const expected = byId("expected-state-version");
      if (expected) expected.value = state.version.toString();
      if (events.length > 0) {
        await refreshSnapshot();
      }
    } catch (error) {
      if (error.status === 409 || error.status === 410) {
        await refreshSnapshot({announceRefresh: true});
      } else {
        state.snapshotReady = false;
        state.sessionIdentity = null;
        setCommandAvailability(false);
        text("freshness", "Host synchronization unavailable; displayed values may be stale.");
        announce("Host synchronization failed. Displayed values may be stale.", true);
      }
    } finally {
      state.polling = false;
    }
  }

  async function submitCommand(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');

    if (!state.snapshotReady || state.sessionIdentity === null) {
      setCommandAvailability(false);
      text(
        "command-result",
        "Command blocked until the authenticated host session and canonical snapshot are available.");
      byId("command-result").focus();
      return;
    }

    const action = byId("host-action").value;
    const commandId = crypto.randomUUID();
    const payload = {
      command_id: commandId,
      idempotency_key: crypto.randomUUID(),
      expected_state_version: state.version.toString(),
      actor: state.sessionIdentity.actor,
      session: state.sessionIdentity.session,
      action,
      payload: {}
    };

    button.disabled = true;
    text("command-result", "Submitting host command.");
    try {
      const result = await submitCanonicalCommand(payload);
      if (result.status === "ACCEPTED") {
        text(
          "command-result",
          "Command " + commandId +
            " was accepted for processing. It is not yet a completed financial outcome.");
      } else if (result.status === "CONFLICT") {
        text(
          "command-result",
          "Command " + commandId +
            " was not accepted because host state changed. Refresh and review before retrying.");
      } else {
        const reasons = result.reasonCodes.length > 0
          ? " Reasons: " + result.reasonCodes.join(", ") + "."
          : "";
        text(
          "command-result",
          "Command " + commandId + " was rejected by the host." + reasons);
      }
      byId("command-result").focus();
      try {
        await refreshSnapshot();
      } catch {
        state.snapshotReady = false;
        state.sessionIdentity = null;
        setCommandAvailability(false);
        announce(
          "Host state refresh failed after the command response. The confirmed command response remains unchanged.",
          true);
      }
    } catch {
      state.snapshotReady = false;
      state.sessionIdentity = null;
      setCommandAvailability(false);
      text(
        "command-result",
        "The host command could not be confirmed. No durable financial or safety outcome is being claimed.");
      byId("command-result").focus();
    } finally {
      setCommandAvailability(state.snapshotReady && state.sessionIdentity !== null);
    }
  }

  async function start() {
    byId("host-command-form").addEventListener("submit", submitCommand);
    setCommandAvailability(false);
    try {
      await refreshSnapshot();
    } catch {
      state.snapshotReady = false;
      state.sessionIdentity = null;
      setCommandAvailability(false);
      text("freshness", "Host unavailable; no current state has been confirmed.");
      announce("Host unavailable. No current state has been confirmed.", true);
    }
    window.setInterval(pollEvents, 2000);
  }

  window.addEventListener("pagehide", () => {
    state.stopped = true;
  });

  document.addEventListener("DOMContentLoaded", start);
})();
