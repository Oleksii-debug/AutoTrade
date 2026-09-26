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
    REVOKE_AUTHORITY: new Set(["OWNER"])
  });
  const TABLE_TOOLS = Object.freeze([
    Object.freeze({
      bodyId: "strategy-body",
      filterId: "strategy-filter",
      copyId: "strategy-copy",
      statusId: "strategy-filter-status",
      label: "strategy and decision"
    }),
    Object.freeze({
      bodyId: "portfolio-body",
      filterId: "portfolio-filter",
      copyId: "portfolio-copy",
      statusId: "portfolio-filter-status",
      label: "portfolio"
    }),
    Object.freeze({
      bodyId: "risk-body",
      filterId: "risk-filter",
      copyId: "risk-copy",
      statusId: "risk-filter-status",
      label: "risk and authority"
    }),
    Object.freeze({
      bodyId: "jobs-body",
      filterId: "jobs-filter",
      copyId: "jobs-copy",
      statusId: "jobs-filter-status",
      label: "research and replay jobs"
    }),
    Object.freeze({
      bodyId: "event-history-body",
      filterId: "event-history-filter",
      copyId: "event-history-copy",
      statusId: "event-history-filter-status",
      label: "received host events"
    })
  ]);

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
    "strategy-filter",
    "strategy-copy",
    "portfolio-filter",
    "portfolio-copy",
    "risk-filter",
    "risk-copy",
    "jobs-filter",
    "jobs-copy",
    "event-history-filter",
    "event-history-copy",
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

  function syncHostActionOptions(role) {
    const select = byId("host-action");
    if (!select) return false;
    let firstAllowed = null;
    for (const option of select.options) {
      const allowed = roleCanSubmitAction(role, option.value);
      option.disabled = !allowed;
      if (allowed && firstAllowed === null) firstAllowed = option.value;
    }
    if (!roleCanSubmitAction(role, select.value) && firstAllowed !== null) {
      select.value = firstAllowed;
    }
    return firstAllowed !== null;
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
      roleCanSubmitAction(state.sessionIdentity.role, effectiveAction);
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
    row.dataset.filterableRow = "true";
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
      reapplyTableFilter(bodyId);
      return;
    }
    for (const [key, value] of entries) {
      appendProjectionRow(body, key, value);
    }
    reapplyTableFilter(bodyId);
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
      reapplyTableFilter("jobs-body");
      return;
    }
    jobs.forEach((job, index) => {
      const row = document.createElement("tr");
      row.dataset.filterableRow = "true";
      const header = document.createElement("th");
      header.scope = "row";
      header.textContent = "Job " + String(index + 1);
      const cell = document.createElement("td");
      cell.textContent = projectionText(job);
      row.append(header, cell);
      body.appendChild(row);
    });
    reapplyTableFilter("jobs-body");
  }

  function normalizedTableQuery(value) {
    return String(value ?? "").trim().toLocaleLowerCase();
  }

  function tableSearchText(row) {
    return [...row.cells]
      .map((cell) => cell.textContent.replace(/\s+/g, " ").trim())
      .join(" ")
      .toLocaleLowerCase();
  }

  function toolForBody(bodyId) {
    return TABLE_TOOLS.find((tool) => tool.bodyId === bodyId) || null;
  }

  function filterableRows(body) {
    return [...body.querySelectorAll('tr[data-filterable-row="true"]')];
  }

  function applyTableFilter(tool) {
    const body = byId(tool.bodyId);
    const filter = byId(tool.filterId);
    if (!body || !filter) return;
    const rows = filterableRows(body);
    const query = normalizedTableQuery(filter.value);
    let visible = 0;
    for (const row of rows) {
      const matches = query === "" || tableSearchText(row).includes(query);
      row.hidden = !matches;
      if (matches) visible += 1;
    }
    if (rows.length === 0) {
      text(tool.statusId, "No host rows are available to filter.");
    } else if (query === "") {
      text(tool.statusId, String(rows.length) + " rows shown.");
    } else {
      text(
        tool.statusId,
        String(visible) + " of " + String(rows.length) +
          " rows match the current filter.");
    }
  }

  function reapplyTableFilter(bodyId) {
    const tool = toolForBody(bodyId);
    if (tool !== null) applyTableFilter(tool);
  }

  function visibleTableRows(tool) {
    const body = byId(tool.bodyId);
    if (!body) return [];
    return filterableRows(body).filter((row) => !row.hidden);
  }

  function tabSeparatedRowText(row) {
    return [...row.cells]
      .map((cell) => cell.textContent.replace(/\s+/g, " ").trim())
      .join("\t");
  }

  async function copyVisibleTableRows(tool) {
    const rows = visibleTableRows(tool);
    if (rows.length === 0) {
      text(tool.statusId, "No visible " + tool.label + " rows are available to copy.");
      return;
    }
    if (!navigator.clipboard ||
        typeof navigator.clipboard.writeText !== "function") {
      text(
        tool.statusId,
        "Clipboard access is unavailable. Use normal text selection and copy.");
      return;
    }
    const payload = rows.map((row) => tabSeparatedRowText(row)).join("\n");
    try {
      await navigator.clipboard.writeText(payload);
      text(
        tool.statusId,
        String(rows.length) + " visible " + tool.label + " rows copied.");
    } catch {
      text(
        tool.statusId,
        "Clipboard copy was not permitted. Use normal text selection and copy.");
    }
  }

  function bindTableTools() {
    for (const tool of TABLE_TOOLS) {
      const filter = byId(tool.filterId);
      const copy = byId(tool.copyId);
      if (!filter || !copy) continue;
      filter.addEventListener("input", () => applyTableFilter(tool));
      copy.addEventListener("click", () => {
        void copyVisibleTableRows(tool);
      });
      applyTableFilter(tool);
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
    row.dataset.filterableRow = "true";
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
    reapplyTableFilter("event-history-body");
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
    reapplyTableFilter("event-history-body");
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
      payload: Object.freeze({})
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
    const payload = commandForSubmission(action);
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
    bindTableTools();
    byId("host-command-form").addEventListener("submit", submitCommand);
    byId("host-action").addEventListener("change", () => {
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
