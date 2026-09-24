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
    polling: false,
    stopped: false,
    announcementTimer: null,
    pendingAnnouncements: []
  };

  const byId = (id) => document.getElementById(id);

  function exactCounter(value, name) {
    if (typeof value === "bigint") {
      if (value < 0n) throw new Error(`${name} must be non-negative`);
      return value;
    }
    if (typeof value === "number") {
      if (!Number.isSafeInteger(value) || value < 0) {
        throw new Error(`${name} must be an exact non-negative integer`);
      }
      return BigInt(value);
    }
    const token = String(value ?? "0");
    if (!/^(0|[1-9][0-9]*)$/.test(token)) {
      throw new Error(`${name} must be a canonical non-negative integer`);
    }
    return BigInt(token);
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

  function operationEntries(snapshot) {
    const operations = snapshot && snapshot.operations;
    if (!operations || typeof operations !== "object" || Array.isArray(operations)) {
      return [];
    }
    return Object.entries(operations).sort(([left], [right]) =>
      left.localeCompare(right));
  }

  function renderOperations(snapshot) {
    const body = byId("operations-body");
    if (!body) return;
    const entries = operationEntries(snapshot);
    const focusedId = document.activeElement && document.activeElement.id;
    const fragment = document.createDocumentFragment();
    if (entries.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 2;
      cell.textContent = "No operations are currently recorded.";
      row.append(cell);
      fragment.append(row);
    } else {
      for (const [operationId, phase] of entries) {
        const row = document.createElement("tr");
        const idCell = document.createElement("td");
        const phaseCell = document.createElement("td");
        idCell.textContent = operationId;
        phaseCell.textContent = String(phase);
        row.append(idCell, phaseCell);
        fragment.append(row);
      }
    }
    body.replaceChildren(fragment);
    if (focusedId) {
      const prior = document.getElementById(focusedId);
      if (prior && document.activeElement !== prior) prior.focus();
    }
  }

  function renderSnapshot(snapshot, {announceRefresh = false} = {}) {
    const nextVersion = exactCounter(
      snapshot.state_version ?? "0",
      "state_version");
    const nextCursor = exactCounter(
      snapshot.event_cursor ?? "0",
      "event_cursor");
    if (nextVersion < state.version || nextCursor < state.cursor) {
      throw new Error("host snapshot counters regressed");
    }
    state.version = nextVersion;
    state.cursor = nextCursor;
    text("state-version", state.version.toString());
    text("event-cursor", state.cursor.toString());
    const expected = byId("expected-state-version");
    if (expected) expected.value = state.version.toString();

    const host = snapshot.active_host ?? snapshot.host ?? "Unavailable";
    const account = snapshot.account_id ?? snapshot.account ?? "Unavailable";
    const environment = snapshot.environment ?? "Unavailable";
    text("active-host", host);
    text("active-account", account);
    text("active-environment", environment);
    text("connection-summary", `Host: ${host}. Account: ${account}. Environment: ${environment}.`);

    const stale = snapshot.stale === true || snapshot.ready === false;
    const freshness = stale
      ? `Stale or not ready. Last evidence: ${snapshot.last_evidence_at ?? "Unavailable"}.`
      : `Current as of ${snapshot.last_evidence_at ?? "host snapshot"}.`;
    text("freshness", freshness);
    renderOperations(snapshot);
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
    const action = byId("host-action").value;
    const commandId = crypto.randomUUID();
    const payload = {
      command_id: commandId,
      idempotency_key: crypto.randomUUID(),
      expected_state_version: state.version.toString(),
      action,
      payload: {}
    };
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    text("command-result", "Submitting host command.");
    try {
      const result = await jsonFetch(`${API}/commands`, {
        method: "POST",
        body: JSON.stringify(payload)
      });
      const status = String(result.status ?? "UNKNOWN");
      if (status === "ACCEPTED") {
        text(
          "command-result",
          `Command ${commandId} was accepted for processing. It is not yet a completed financial outcome.`);
      } else if (status === "CONFLICT") {
        text(
          "command-result",
          `Command ${commandId} was not accepted because host state changed. Refresh and review before retrying.`);
      } else {
        text("command-result", `Command ${commandId} returned status ${status}.`);
      }
      byId("command-result").focus();
      try {
        await refreshSnapshot();
      } catch {
        announce(
          "Host state refresh failed after the command response. The confirmed command response remains unchanged.",
          true);
      }
    } catch {
      text(
        "command-result",
        "The host command could not be confirmed. No durable financial or safety outcome is being claimed.");
      byId("command-result").focus();
    } finally {
      button.disabled = false;
    }
  }

  async function start() {
    byId("host-command-form").addEventListener("submit", submitCommand);
    try {
      await refreshSnapshot();
    } catch {
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
