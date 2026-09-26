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
  const HOST_ACTION_ROLES = Object.freeze({
    BLOCK_NEW_EXPOSURE: new Set(["OWNER", "OPERATOR"]),
    REVOKE_AUTHORITY: new Set(["OWNER"]),
    SET_AUTHORITY: new Set(["OWNER"])
  });

  const state = {
    cursor: 0n,
    version: 0n,
    sessionIdentity: null,
    accountId: null,
    environment: null,
    snapshotReady: false,
    polling: false,
    stopped: false,
    announcementTimer: null,
    pendingAnnouncements: [],
    urgentAnnouncementTimer: null,
    pendingUrgentAnnouncements: [],
    restoreFocusId: null,
    pendingCommand: null
  };

  const byId = (id) => document.getElementById(id);
  const RESTORABLE_FOCUS_IDS = new Set([
    "main",
    "permissions-region",
    "strategy-region",
    "portfolio-region",
    "operations-region",
    "risk-region",
    "jobs-region",
    "event-history-region",
    "host-action",
    "authority-policy-id",
    "authority-instrument-id",
    "authority-instrument-version",
    "authority-actions",
    "authority-max-notional",
    "authority-valid-from",
    "authority-expires-at",
    "authority-policy-version",
    "authority-policy-confirm",
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

  function parsePermissionSummary(value) {
    const permissionSummary = requiredObject(value, "permission_summary");
    const allowed = new Set(["actor", "session", "role", "capabilities"]);
    for (const key of Object.keys(permissionSummary)) {
      if (!allowed.has(key)) {
        throw new Error("permission_summary contains non-canonical field " + key);
      }
    }

    const actor = requiredText(
      permissionSummary.actor, "permission_summary.actor");
    const session = requiredText(
      permissionSummary.session, "permission_summary.session");
    if (!/^sid-[0-9a-f]{64}$/.test(session)) {
      throw new Error(
        "permission_summary.session must be a canonical public session reference");
    }
    const role = requiredText(
      permissionSummary.role, "permission_summary.role");
    const capabilities = permissionSummary.capabilities === undefined
      ? []
      : requiredStringArray(
        permissionSummary.capabilities, "permission_summary.capabilities");
    if (capabilities.some((item) => item !== item.trim()) ||
        new Set(capabilities).size !== capabilities.length) {
      throw new Error(
        "permission_summary.capabilities must contain unique canonical strings");
    }
    return {actor, session, role, capabilities};
  }

  function readSessionIdentity(permissionSummary) {
    const actor = permissionSummary.actor;
    const session = permissionSummary.session;
    return {
      actor: requiredText(actor, "permission_summary.actor"),
      session: requiredText(session, "permission_summary.session"),
      role: requiredText(permissionSummary.role, "permission_summary.role")
    };
  }

  function roleCanSubmitAction(role, action) {
    const allowedRoles = HOST_ACTION_ROLES[action];
    return allowedRoles instanceof Set && allowedRoles.has(role);
  }

  function actionCanSubmitInCurrentScope(role, action) {
    if (!roleCanSubmitAction(role, action)) return false;
    if (action === "SET_AUTHORITY" && state.environment === "REPLAY") return false;
    return true;
  }

  const AUTHORITY_POLICY_REQUIRED_FIELD_IDS = Object.freeze([
    "authority-policy-id",
    "authority-instrument-id",
    "authority-instrument-version",
    "authority-actions",
    "authority-max-notional",
    "authority-valid-from",
    "authority-expires-at",
    "authority-policy-version"
  ]);

  function authorityReviewScopeKey() {
    const actor = state.sessionIdentity === null ? "" : state.sessionIdentity.actor;
    const session = state.sessionIdentity === null ? "" : state.sessionIdentity.session;
    return [
      actor,
      session,
      String(state.accountId ?? ""),
      String(state.environment ?? "")
    ].join("\n");
  }

  function invalidateAuthorityPolicyReview() {
    const confirmation = byId("authority-policy-confirm");
    if (!confirmation) return;
    confirmation.checked = false;
    delete confirmation.dataset.reviewStateVersion;
    delete confirmation.dataset.reviewScope;
    delete confirmation.dataset.reviewCommandId;
  }

  function bindAuthorityPolicyReviewInvalidation() {
    for (const id of [
      ...AUTHORITY_POLICY_REQUIRED_FIELD_IDS,
      "authority-autonomous",
      "authority-protection-only"
    ]) {
      const input = byId(id);
      if (!input) continue;
      input.addEventListener("input", invalidateAuthorityPolicyReview);
      input.addEventListener("change", invalidateAuthorityPolicyReview);
    }
    const confirmation = byId("authority-policy-confirm");
    if (!confirmation) return;
    confirmation.addEventListener("change", () => {
      if (!confirmation.checked || !state.snapshotReady) {
        invalidateAuthorityPolicyReview();
        return;
      }
      confirmation.dataset.reviewStateVersion = state.version.toString();
      confirmation.dataset.reviewScope = authorityReviewScopeKey();
      confirmation.dataset.reviewCommandId =
        state.pendingCommand !== null &&
        state.pendingCommand.action === "SET_AUTHORITY"
          ? state.pendingCommand.command_id
          : "";
    });
  }

  function syncAuthorityPolicyFields(action) {
    const container = byId("authority-policy-fields");
    if (!container) return;
    const active = action === "SET_AUTHORITY";
    const lockedForRetry = active &&
      state.pendingCommand !== null &&
      state.pendingCommand.action === "SET_AUTHORITY";
    const scopeKey = active ? authorityReviewScopeKey() : "";
    const priorScopeKey = container.dataset.authorityScopeKey || "";
    if (!active || (priorScopeKey !== "" && priorScopeKey !== scopeKey)) {
      invalidateAuthorityPolicyReview();
    }
    container.dataset.authorityScopeKey = scopeKey;
    container.hidden = !active;
    for (const id of AUTHORITY_POLICY_REQUIRED_FIELD_IDS) {
      const input = byId(id);
      if (!input) continue;
      input.required = active;
      input.disabled = !active || lockedForRetry;
    }
    for (const id of [
      "authority-autonomous",
      "authority-protection-only",
      "authority-policy-confirm"
    ]) {
      const input = byId(id);
      if (!input) continue;
      input.disabled = !active || (lockedForRetry && id !== "authority-policy-confirm");
      input.required = active && id === "authority-policy-confirm";
    }
    text("authority-policy-account", active ? state.accountId : null);
    text("authority-policy-environment", active ? state.environment : null);
  }

  function syncHostActionOptions(role) {
    const select = byId("host-action");
    if (!select) return false;
    let firstAllowed = null;
    for (const option of select.options) {
      const allowed = actionCanSubmitInCurrentScope(role, option.value);
      option.disabled = !allowed;
      if (allowed && firstAllowed === null) firstAllowed = option.value;
    }
    if (state.pendingCommand !== null) {
      select.value = state.pendingCommand.action;
      select.disabled = true;
    } else {
      select.disabled = false;
      if (!actionCanSubmitInCurrentScope(role, select.value) && firstAllowed !== null) {
        select.value = firstAllowed;
      }
    }
    syncAuthorityPolicyFields(select.value);
    return firstAllowed !== null;
  }

  function renderPendingAuthorityPolicyForRetry() {
    if (state.pendingCommand === null ||
        state.pendingCommand.action !== "SET_AUTHORITY") {
      return;
    }
    const policy = requiredObject(
      state.pendingCommand.payload, "pending SET_AUTHORITY payload");
    if (!Array.isArray(policy.environments) ||
        policy.environments.length !== 1 ||
        policy.environments[0] !== state.pendingCommand.environment) {
      throw new Error("pending authority policy environment is not canonical");
    }
    if (!Array.isArray(policy.instruments) || policy.instruments.length !== 1) {
      throw new Error("pending authority policy must contain exactly one instrument");
    }
    const instrument = requiredObject(
      policy.instruments[0], "pending authority instrument");
    if (!Array.isArray(policy.actions) || policy.actions.length === 0) {
      throw new Error("pending authority policy actions are unavailable");
    }

    byId("authority-policy-id").value = String(policy.policy_id);
    byId("authority-instrument-id").value = String(instrument.instrument_id);
    byId("authority-instrument-version").value = String(instrument.version);
    byId("authority-actions").value = policy.actions.join(",");
    byId("authority-max-notional").value = String(policy.max_notional);
    byId("authority-valid-from").value = String(policy.valid_from);
    byId("authority-expires-at").value = String(policy.expires_at);
    byId("authority-policy-version").value = String(policy.version);
    byId("authority-autonomous").checked = policy.autonomous === true;
    byId("authority-protection-only").checked = policy.protection_only === true;
    text("authority-policy-account", state.pendingCommand.account_id);
    text("authority-policy-environment", state.pendingCommand.environment);
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
    const permissionSummary = parsePermissionSummary(
      snapshot.permission_summary);
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
    const action = byId("host-action");
    const effectiveAction = state.pendingCommand !== null
      ? state.pendingCommand.action
      : (action === null ? null : action.value);
    const roleAllowed = state.sessionIdentity !== null &&
      effectiveAction !== null &&
      actionCanSubmitInCurrentScope(state.sessionIdentity.role, effectiveAction);
    if (button) button.disabled = !enabled || !roleAllowed;
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

  function stableProjectionValue(value) {
    if (Array.isArray(value)) {
      return value.map((item) => stableProjectionValue(item));
    }
    if (value && typeof value === "object") {
      const ordered = {};
      for (const key of Object.keys(value).sort()) {
        ordered[key] = stableProjectionValue(value[key]);
      }
      return ordered;
    }
    return value;
  }

  function projectionText(value) {
    if (value === null) return "null";
    if (value && typeof value === "object") {
      return JSON.stringify(stableProjectionValue(value));
    }
    return String(value);
  }

  function flattenProjectionRows(record) {
    const rows = [];

    function visit(value, path) {
      if (Array.isArray(value)) {
        if (value.length === 0) {
          rows.push([path, "[]"]);
          return;
        }
        value.forEach((item, index) => {
          visit(item, path + "[" + String(index + 1) + "]");
        });
        return;
      }
      if (value && typeof value === "object") {
        const keys = Object.keys(value).sort();
        if (keys.length === 0) {
          rows.push([path, "{}"]);
          return;
        }
        for (const key of keys) {
          visit(value[key], path ? path + "." + key : key);
        }
        return;
      }
      rows.push([path, projectionText(value)]);
    }

    for (const key of Object.keys(record).sort()) {
      visit(record[key], key);
    }
    return rows;
  }

  function appendProjectionRow(body, label, value) {
    const row = document.createElement("tr");
    const header = document.createElement("th");
    header.scope = "row";
    header.textContent = label;
    const cell = document.createElement("td");
    cell.textContent = value;
    row.append(header, cell);
    body.appendChild(row);
  }

  function renderProjection(bodyId, record, emptyMessage) {
    const body = byId(bodyId);
    if (!body) return;
    body.replaceChildren();
    const entries = flattenProjectionRows(record);
    if (entries.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 2;
      cell.textContent = emptyMessage;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const [key, value] of entries) {
      appendProjectionRow(body, key, value);
    }
  }

  function renderPermissionSummary(permissionSummary) {
    const body = byId("permissions-body");
    if (!body) return;
    body.replaceChildren();
    appendProjectionRow(body, "Actor", permissionSummary.actor);
    appendProjectionRow(body, "Session", permissionSummary.session);
    appendProjectionRow(body, "Role", permissionSummary.role);
    if (permissionSummary.capabilities.length === 0) {
      appendProjectionRow(
        body, "Capabilities", "No capabilities reported by the host snapshot.");
      return;
    }
    permissionSummary.capabilities.forEach((capability, index) => {
      appendProjectionRow(
        body, "Capability " + String(index + 1), capability);
    });
  }

  function renderJobs(jobs) {
    const body = byId("jobs-body");
    if (!body) return;
    body.replaceChildren();
    if (jobs.length === 0) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 2;
      cell.textContent = "No background jobs reported by the host snapshot.";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    jobs.forEach((job, index) => {
      const row = document.createElement("tr");
      const header = document.createElement("th");
      header.scope = "row";
      header.textContent = "Job " + String(index + 1);
      const cell = document.createElement("td");
      cell.textContent = projectionText(job);
      row.append(header, cell);
      body.appendChild(row);
    });
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
      state.pendingUrgentAnnouncements.push(message);
      if (state.urgentAnnouncementTimer !== null) return;
      state.urgentAnnouncementTimer = window.setTimeout(() => {
        const pending = state.pendingUrgentAnnouncements;
        state.pendingUrgentAnnouncements = [];
        state.urgentAnnouncementTimer = null;
        // Preserve every urgent event in the burst, including identical ones,
        // while producing one stable assertive live-region mutation for NVDA.
        announceLiveText("urgent-status", pending.join(" "));
      }, 0);
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

  function renderHostEvent(event, cursor, stateVersion) {
    const body = byId("event-history-body");
    if (!body) return;
    const kind = requiredText(
      event.kind ?? event.event_type,
      "event.kind");
    const payload = event.payload === undefined ? {} : event.payload;

    if (body.children.length === 1 &&
        body.firstElementChild.dataset.hostEventCursor === undefined) {
      body.replaceChildren();
    }

    const row = document.createElement("tr");
    row.dataset.hostEventCursor = cursor.toString();
    for (let index = 0; index < 4; index += 1) {
      row.appendChild(document.createElement("td"));
    }
    row.children[0].textContent = cursor.toString();
    row.children[1].textContent = stateVersion.toString();
    row.children[2].textContent = kind;
    row.children[3].textContent = projectionText(payload);
    body.prepend(row);

    while (body.children.length > 100) {
      body.lastElementChild.remove();
    }
  }

  function resetEventHistoryForScope() {
    const body = byId("event-history-body");
    if (!body) return;
    body.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.textContent = "No canonical host events received in this account/environment session.";
    row.appendChild(cell);
    body.appendChild(row);
  }

  function renderSnapshot(snapshot, {announceRefresh = false} = {}) {
    const parsed = parseCanonicalSnapshot(snapshot);
    const scopeChanged = state.accountId !== null && (
      parsed.accountId !== state.accountId ||
      parsed.environment !== state.environment);
    if (scopeChanged) {
      state.cursor = 0n;
      state.version = 0n;
      resetEventHistoryForScope();
    }
    if (parsed.version < state.version || parsed.cursor < state.cursor) {
      throw new Error("host snapshot counters regressed");
    }

    state.version = parsed.version;
    state.cursor = parsed.cursor;
    state.sessionIdentity = parsed.sessionIdentity;
    state.accountId = parsed.accountId;
    state.environment = parsed.environment;
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
    text("server-time", parsed.serverTime);
    renderPermissionSummary(parsed.permissionSummary);
    renderProjection(
      "portfolio-body",
      parsed.portfolio,
      "No portfolio projection reported by the host snapshot.");
    renderProjection(
      "risk-body",
      parsed.risk,
      "No risk projection reported by the host snapshot.");
    renderProjection(
      "strategy-body",
      parsed.strategy,
      "No strategy or decision projection reported by the host snapshot.");
    renderJobs(parsed.jobs);

    const hasAllowedAction = parsed.sessionIdentity !== null &&
      syncHostActionOptions(parsed.sessionIdentity.role);
    renderPendingAuthorityPolicyForRetry();
    const canSubmit = parsed.sessionIdentity !== null && hasAllowedAction;
    setCommandAvailability(canSubmit);
    if (!canSubmit) {
      text(
        "command-result",
        parsed.sessionIdentity === null
          ? "Authenticated host session identity is unavailable. Commands remain blocked."
          : "The authenticated role has no permitted host safety command. Commands remain blocked.");
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
        renderHostEvent(event, cursor, version);
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
          state.accountId = null;
          state.environment = null;
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
        state.accountId = null;
        state.environment = null;
        setCommandAvailability(false);
        text("freshness", "Host synchronization unavailable; displayed values may be stale.");
        announce("Host synchronization failed. Displayed values may be stale.", true);
      }
    } finally {
      state.polling = false;
    }
  }

  function requiredPolicyInput(id, name) {
    const input = byId(id);
    if (!input) throw new Error(name + " input is unavailable");
    const value = requiredText(input.value, name);
    if (value !== value.trim()) {
      throw new Error(name + " must not contain surrounding whitespace");
    }
    return value;
  }

  function positiveSafeIntegerPolicyInput(id, name) {
    const token = requiredPolicyInput(id, name);
    if (!/^[1-9][0-9]*$/.test(token)) {
      throw new Error(name + " must be a positive integer");
    }
    const value = Number(token);
    if (!Number.isSafeInteger(value)) {
      throw new Error(name + " exceeds the exact browser integer range");
    }
    return value;
  }

  function positiveDecimalPolicyInput(id, name) {
    const token = requiredPolicyInput(id, name);
    if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(token) ||
        !/[1-9]/.test(token.replace(".", ""))) {
      throw new Error(name + " must be a positive canonical decimal");
    }
    return token;
  }

  function authorityPolicyActions() {
    const raw = requiredPolicyInput("authority-actions", "allowed actions");
    const actions = raw.split(",").map((item) => item.trim()).filter(Boolean);
    if (actions.length === 0 || new Set(actions).size !== actions.length) {
      throw new Error("allowed actions must be non-empty and unique");
    }
    if (actions.some((item) => item !== item.toUpperCase())) {
      throw new Error("allowed actions must use canonical uppercase names");
    }
    return actions;
  }

  function authorityPolicyPayload() {
    if (state.environment === "REPLAY") {
      throw new Error("authority policy activation is unavailable in REPLAY");
    }
    if (!["SIMULATION", "PAPER", "LIVE"].includes(state.environment)) {
      throw new Error("authority policy activation requires a canonical trading environment");
    }
    const confirmation = byId("authority-policy-confirm");
    if (!confirmation || !confirmation.checked) {
      throw new Error("review confirmation is required before authority policy submission");
    }
    const reviewCommandId =
      state.pendingCommand !== null &&
      state.pendingCommand.action === "SET_AUTHORITY"
        ? state.pendingCommand.command_id
        : "";
    if (
      confirmation.dataset.reviewStateVersion !== state.version.toString() ||
      confirmation.dataset.reviewScope !== authorityReviewScopeKey() ||
      confirmation.dataset.reviewCommandId !== reviewCommandId
    ) {
      throw new Error(
        "review confirmation must be renewed after host state or policy scope changes");
    }
    const instrumentId = requiredPolicyInput(
      "authority-instrument-id", "instrument ID");
    if (!/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(
      instrumentId)) {
      throw new Error("instrument ID must be a canonical UUID");
    }
    const validFrom = requiredPolicyInput("authority-valid-from", "valid from");
    const expiresAt = requiredPolicyInput("authority-expires-at", "expires at");
    utcInstant(validFrom, "valid from");
    utcInstant(expiresAt, "expires at");
    if (Date.parse(expiresAt) <= Date.parse(validFrom)) {
      throw new Error("authority policy expiry must be after valid from");
    }
    return Object.freeze({
      policy_id: requiredPolicyInput("authority-policy-id", "policy ID"),
      environments: Object.freeze([state.environment]),
      instruments: Object.freeze([Object.freeze({
        instrument_id: instrumentId.toLowerCase(),
        version: positiveSafeIntegerPolicyInput(
          "authority-instrument-version", "instrument version")
      })]),
      actions: Object.freeze(authorityPolicyActions()),
      max_notional: positiveDecimalPolicyInput(
        "authority-max-notional", "maximum notional"),
      expires_at: expiresAt,
      autonomous: byId("authority-autonomous").checked,
      valid_from: validFrom,
      protection_only: byId("authority-protection-only").checked,
      version: positiveSafeIntegerPolicyInput(
        "authority-policy-version", "policy version")
    });
  }

  function commandActionPayload(action) {
    if (action === "SET_AUTHORITY") return authorityPolicyPayload();
    return Object.freeze({});
  }

  function newCommandPayload(action) {
    const commandId = crypto.randomUUID();
    return Object.freeze({
      command_id: commandId,
      idempotency_key: crypto.randomUUID(),
      expected_state_version: state.version.toString(),
      actor: state.sessionIdentity.actor,
      session: state.sessionIdentity.session,
      account_id: state.accountId,
      environment: state.environment,
      action,
      payload: commandActionPayload(action)
    });
  }

  function commandForSubmission(action) {
    if (state.pendingCommand !== null) {
      return state.pendingCommand;
    }
    const payload = newCommandPayload(action);
    state.pendingCommand = payload;
    return payload;
  }

  function clearConfirmedCommand(payload) {
    if (state.pendingCommand === payload) {
      state.pendingCommand = null;
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
    const recovering = state.pendingCommand !== null;
    if (recovering && (
        state.pendingCommand.actor !== state.sessionIdentity.actor ||
        state.pendingCommand.session !== state.sessionIdentity.session ||
        state.pendingCommand.account_id !== state.accountId ||
        state.pendingCommand.environment !== state.environment)) {
      setCommandAvailability(false);
      text(
        "command-result",
        "Unresolved command scope no longer matches the authenticated host snapshot. " +
          "The original command identity is preserved and will not be retargeted.");
      byId("command-result").focus();
      return;
    }
    if (recovering && !roleCanSubmitAction(
        state.sessionIdentity.role,
        state.pendingCommand.action)) {
      setCommandAvailability(false);
      text(
        "command-result",
        "The authenticated role no longer permits the unresolved command. " +
          "Its original identity is preserved, but the browser will not retry it.");
      byId("command-result").focus();
      return;
    }
    let payload;
    try {
      if (recovering && state.pendingCommand.action === "SET_AUTHORITY") {
        const reviewedPolicy = authorityPolicyPayload();
        if (JSON.stringify(reviewedPolicy) !==
            JSON.stringify(state.pendingCommand.payload)) {
          throw new Error(
            "reviewed authority policy does not exactly match the unresolved command payload");
        }
      }
      payload = commandForSubmission(action);
    } catch (error) {
      const message = error instanceof Error ? error.message : "invalid authority command";
      text("command-result", "Command was not submitted: " + message + ".");
      byId("command-result").focus();
      setCommandAvailability(state.snapshotReady && state.sessionIdentity !== null);
      return;
    }
    const commandId = payload.command_id;
    if (recovering && action !== payload.action) {
      byId("host-action").value = payload.action;
    }

    button.disabled = true;
    text(
      "command-result",
      recovering
        ? "Retrying unresolved command " + commandId +
          " with its original idempotency identity. No new command is being created."
        : "Submitting host command " + commandId + ".");
    try {
      const result = await submitCanonicalCommand(payload);
      clearConfirmedCommand(payload);
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
        state.accountId = null;
        state.environment = null;
        setCommandAvailability(false);
        announce(
          "Host state refresh failed after the command response. The confirmed command response remains unchanged.",
          true);
      }
    } catch {
      state.snapshotReady = false;
      state.sessionIdentity = null;
      state.accountId = null;
      state.environment = null;
      setCommandAvailability(false);
      text(
        "command-result",
        "Command " + commandId +
          " could not be confirmed. Its original command_id and idempotency_key are retained for exact retry after host state recovers. No durable financial or safety outcome is being claimed.");
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
      state.accountId = null;
      state.environment = null;
      setCommandAvailability(false);
      text("freshness", "Host unavailable; displayed values may be stale.");
      announce("Host state refresh failed. Displayed values may be stale.", true);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function start() {
    bindAuthorityPolicyReviewInvalidation();
    byId("host-command-form").addEventListener("submit", submitCommand);
    byId("host-action").addEventListener("change", () => {
      syncAuthorityPolicyFields(byId("host-action").value);
      setCommandAvailability(state.snapshotReady && state.sessionIdentity !== null);
    });
    byId("refresh-state").addEventListener("click", refreshStateFromUser);
    setCommandAvailability(false);
    try {
      await refreshSnapshot();
    } catch {
      state.snapshotReady = false;
      state.sessionIdentity = null;
      state.accountId = null;
      state.environment = null;
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
    state.accountId = null;
    state.environment = null;
    setCommandAvailability(false);
  });

  window.addEventListener("pageshow", async (event) => {
    if (!event.persisted) return;
    state.stopped = false;
    state.snapshotReady = false;
    state.sessionIdentity = null;
    state.accountId = null;
    state.environment = null;
    setCommandAvailability(false);
    try {
      await refreshSnapshot();
      announce("Host state refreshed after page restoration.");
    } catch {
      state.snapshotReady = false;
      state.sessionIdentity = null;
      state.accountId = null;
      state.environment = null;
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
