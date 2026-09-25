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
    pendingAnnouncements: [],
    restoreFocusId: null
  };

  const byId = (id) => document.getElementById(id);
  const RESTORABLE_FOCUS_IDS = new Set([
    "main",
    "operations-region",
    "host-action",
    "submit-command",
    "refresh-state",
    "command-result"
  ]);

  function captureFocusForRestoration() {
    const active = document.activeElement;
    state.restoreFocusId = active && RESTORABLE_FOCUS_IDS.has(active.id)
      ? active.id
      : null;
  }

  function restoreFocusAfterPageRestore() {
    const focusId = state.restoreFocusId;
    state.restoreFocusId = null;
    if (focusId === null) return;
    const target = byId(focusId);
    if (!target || target.disabled) return;
    target.focus();
  }

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

  function canonicalEnvironment(value, name) {
    const token = requiredText(value, name);
    if (!["REPLAY", "SIMULATION", "PAPER", "LIVE"].includes(token)) {
      throw new Error(name + " is not a canonical environment");
    }
    return token;
  }

  function utcInstant(value, name) {
    const token = requiredText(value, name);
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$/.exec(token);
    if (match === null) {
      throw new Error(name + " must be a canonical UTC instant");
    }
    const [, year, month, day, hour, minute, second] = match;
    const parts = [year, month, day, hour, minute, second].map(Number);
    const [y, m, d, h, min, s] = parts;
    const probe = new Date(Date.UTC(y, m - 1, d, h, min, s));
    if (
      probe.getUTCFullYear() !== y ||
      probe.getUTCMonth() !== m - 1 ||
      probe.getUTCDate() !== d ||
      probe.getUTCHours() !== h ||
      probe.getUTCMinutes() !== min ||
      probe.getUTCSeconds() !== s
    ) {
      throw new Error(name + " must be a canonical UTC instant");
    }
    return token;
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
      serverTime: utcInstant(snapshot.server_time, "server_time"),
      hostId: requiredText(snapshot.host_id, "host_id"),
      accountId: requiredText(snapshot.account_id, "account_id"),
      environment: canonicalEnvironment(snapshot.environment, "environment"),
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
    const operationId = result.operation_id === undefined
      ? null
      : requiredText(result.operation_id, "operation_id");
    if (result.status === "ACCEPTED" && operationId === null) {
      throw new Error("ACCEPTED command must provide operation_id for durable tracking");
    }
    return {
      commandId,
      operationId,
      status: result.status,
      stateVersion: exactCounter(result.state_version, "CommandResult.state_version"),
      reasonCodes: requiredStringArray(result.reason_codes, "CommandResult.reason_codes"),
      fieldErrors: result.field_errors
    };
  }

  function parseOperationResult(value, expectedOperationId) {
    const result = requiredObject(value, "OperationResult");
    const allowed = new Set([
      "operation_id", "phase", "started_at", "updated_at",
      "affected_refs", "evidence", "remaining_uncertainty"
    ]);
    for (const key of Object.keys(result)) {
      if (!allowed.has(key)) {
        throw new Error("OperationResult contains non-canonical field " + key);
      }
    }
    const operationId = requiredText(result.operation_id, "operation_id");
    if (operationId !== expectedOperationId) {
      throw new Error("OperationResult operation_id does not match");
    }
    const phases = [
      "QUEUED", "RUNNING", "WAITING_EXTERNAL",
      "SUCCEEDED", "FAILED", "UNKNOWN", "CANCELLED"
    ];
    if (!phases.includes(result.phase)) {
      throw new Error("OperationResult phase is not canonical");
    }
    if (!Array.isArray(result.evidence) ||
        result.evidence.some((item) =>
          !item || typeof item !== "object" || Array.isArray(item))) {
      throw new Error("OperationResult evidence must be an array of objects");
    }
    const remainingUncertainty = requiredStringArray(
      result.remaining_uncertainty,
      "remaining_uncertainty");
    if (result.phase === "UNKNOWN" && remainingUncertainty.length === 0) {
      throw new Error("UNKNOWN operation must preserve explicit remaining uncertainty");
    }
    if (["SUCCEEDED", "FAILED", "CANCELLED"].includes(result.phase) &&
        remainingUncertainty.length > 0) {
      throw new Error("Terminal operation cannot retain unresolved uncertainty");
    }
    const startedAt = utcInstant(result.started_at, "started_at");
    const updatedAt = utcInstant(result.updated_at, "updated_at");
    if (Date.parse(updatedAt) < Date.parse(startedAt)) {
      throw new Error("OperationResult updated_at cannot precede started_at");
    }
    return {
      operationId,
      phase: result.phase,
      startedAt,
      updatedAt,
      affectedRefs: requiredStringArray(result.affected_refs, "affected_refs"),
      evidence: result.evidence,
      remainingUncertainty
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

  function announceLiveText(id, message) {
    const element = byId(id);
    if (!element) return;
    // A repeated identical message must still produce a DOM change so screen
    // readers can announce a second independent material event.
    element.textContent = "";
    window.setTimeout(() => {
      element.textContent = message;
    }, 0);
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
      announceLiveText("urgent-status", message);
      return;
    }

    state.pendingAnnouncements.push(message);
    if (state.announcementTimer !== null) return;
    state.announcementTimer = window.setTimeout(() => {
      const pending = state.pendingAnnouncements;
      state.pendingAnnouncements = [];
      state.announcementTimer = null;
      // Do not deduplicate identical messages inside the aggregation window:
      // two matching material events are still two independent events.
      announceLiveText("polite-status", pending.join(" "));
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

  function renderOperation(operation) {
    const body = byId("operations-body");
    if (!body) return;

    let row = [...body.querySelectorAll("tr")].find(
      (item) => item.dataset.operationId === operation.operationId);
    if (!row) {
      if (body.children.length === 1 &&
          body.firstElementChild.dataset.operationId === undefined) {
        body.replaceChildren();
      }
      row = document.createElement("tr");
      row.dataset.operationId = operation.operationId;
      for (let index = 0; index < 4; index += 1) {
        row.appendChild(document.createElement("td"));
      }
      body.appendChild(row);
    }

    row.children[0].textContent = operation.operationId;
    row.children[1].textContent = operation.phase;
    row.children[2].textContent = operation.updatedAt;
    row.children[3].textContent = operation.remainingUncertainty.length > 0
      ? operation.remainingUncertainty.join(", ")
      : "None reported";
  }

  async function refreshOperation(operationId) {
    const raw = await jsonFetch(
      `${API}/operations/${encodeURIComponent(operationId)}`);
    const operation = parseOperationResult(raw, operationId);
    renderOperation(operation);
    return operation;
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
      // A failed post-event snapshot refresh disables commands but must not
      // require a new event or manual action to recover. Re-establish the
      // canonical snapshot before the next event request whenever local state
      // is marked unready.
      if (!state.snapshotReady) {
        await refreshSnapshot();
      }
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
        const version = exactCounter(event.state_version, "event.state_version");
        if (version < state.version) {
          await refreshSnapshot({announceRefresh: true});
          return;
        }
        const kind = String(event.kind ?? event.event_type ?? "");
        if (kind === "OPERATION_UPDATED") {
          const payload = requiredObject(event.payload, "event.payload");
          const operationId = requiredText(
            payload.operation_id,
            "event.payload.operation_id");
          await refreshOperation(operationId);
        }
        if (MATERIAL_EVENTS.has(kind)) {
          announce(eventMessage(event), URGENT_EVENTS.has(kind));
        }
        // Commit the local event position only after every required side effect
        // for this event succeeded. If processing throws, the next poll retries
        // the same cursor instead of silently acknowledging an unprocessed event.
        state.cursor = cursor;
        expectedCursor = cursor + 1n;
        if (version > state.version) {
          state.version = version;
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
        try {
          await refreshSnapshot({announceRefresh: true});
        } catch {
          state.snapshotReady = false;
          state.sessionIdentity = null;
          setCommandAvailability(false);
          text(
            "freshness",
            "Host synchronization gap could not be recovered; displayed values may be stale.");
          announce(
            "Host synchronization gap recovery failed. Commands remain blocked until a fresh canonical snapshot is available.",
            true);
        }
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
        let acceptedMessage =
          "Command " + commandId +
          " was accepted for processing. It is not yet a completed financial outcome.";
        if (result.operationId !== null) {
          try {
            const operation = await refreshOperation(result.operationId);
            const uncertainty = operation.remainingUncertainty.length > 0
              ? " Remaining uncertainty: " +
                operation.remainingUncertainty.join(", ") + "."
              : "";
            acceptedMessage +=
              " Operation phase is " + operation.phase + "." + uncertainty;
          } catch {
            acceptedMessage +=
              " Current operation status could not be loaded; the accepted command response remains unchanged.";
          }
        }
        text("command-result", acceptedMessage);
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

  async function refreshStateFromUser() {
    const button = byId("refresh-state");
    if (button) button.disabled = true;
    try {
      await refreshSnapshot();
      announce("Host state refreshed from the canonical snapshot.");
    } catch {
      state.snapshotReady = false;
      state.sessionIdentity = null;
      setCommandAvailability(false);
      text("freshness", "Host unavailable; displayed values may be stale.");
      announce("Host state refresh failed. Displayed values may be stale.", true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function start() {
    byId("host-command-form").addEventListener("submit", submitCommand);
    byId("refresh-state").addEventListener("click", refreshStateFromUser);
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
    captureFocusForRestoration();
    state.stopped = true;
    state.snapshotReady = false;
    state.sessionIdentity = null;
    setCommandAvailability(false);
  });

  window.addEventListener("pageshow", async (event) => {
    if (!event.persisted) return;
    state.stopped = false;
    state.snapshotReady = false;
    state.sessionIdentity = null;
    setCommandAvailability(false);
    try {
      await refreshSnapshot();
      announce("Host state refreshed after page restoration.");
    } catch {
      state.snapshotReady = false;
      state.sessionIdentity = null;
      setCommandAvailability(false);
      text("freshness", "Host unavailable after page restoration; displayed values may be stale.");
      announce(
        "Host synchronization failed after page restoration. Commands remain blocked.",
        true);
    } finally {
      restoreFocusAfterPageRestore();
    }
  });

  document.addEventListener("DOMContentLoaded", start);
})();
