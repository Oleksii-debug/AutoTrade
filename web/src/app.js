(() => {
  "use strict";

  const HOST_API = window.AutoTradeHostApi;
  if (!HOST_API || typeof HOST_API.route !== "function") {
    throw new Error("Canonical host API routes are unavailable");
  }
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

  const TABLE_TOOLS = Object.freeze([
    Object.freeze({bodyId: "permissions-body", filterId: "permissions-filter", copyId: "permissions-copy", sortId: "permissions-sort", previousId: "permissions-previous", nextId: "permissions-next", statusId: "permissions-filter-status", label: "permission and capability"}),
    Object.freeze({bodyId: "strategy-body", filterId: "strategy-filter", copyId: "strategy-copy", sortId: "strategy-sort", previousId: "strategy-previous", nextId: "strategy-next", statusId: "strategy-filter-status", label: "strategy and decision"}),
    Object.freeze({bodyId: "portfolio-body", filterId: "portfolio-filter", copyId: "portfolio-copy", sortId: "portfolio-sort", previousId: "portfolio-previous", nextId: "portfolio-next", statusId: "portfolio-filter-status", label: "portfolio"}),
    Object.freeze({bodyId: "operations-body", filterId: "operations-filter", copyId: "operations-copy", sortId: "operations-sort", previousId: "operations-previous", nextId: "operations-next", statusId: "operations-filter-status", label: "current host operation"}),
    Object.freeze({bodyId: "risk-body", filterId: "risk-filter", copyId: "risk-copy", sortId: "risk-sort", previousId: "risk-previous", nextId: "risk-next", statusId: "risk-filter-status", label: "risk and authority"}),
    Object.freeze({bodyId: "jobs-body", filterId: "jobs-filter", copyId: "jobs-copy", sortId: "jobs-sort", previousId: "jobs-previous", nextId: "jobs-next", statusId: "jobs-filter-status", label: "research and replay jobs"}),
    Object.freeze({bodyId: "event-history-body", filterId: "event-history-filter", copyId: "event-history-copy", sortId: "event-history-sort", previousId: "event-history-previous", nextId: "event-history-next", statusId: "event-history-filter-status", label: "received host events"})
  ]);
  const TABLE_PAGE_SIZE = 25;
  const tableViewState = new Map();

  const state = {
    cursor: 0n,
    version: 0n,
    sessionIdentity: null,
    accountId: null,
    environment: null,
    renderedHostId: null,
    renderedAccountId: null,
    renderedEnvironment: null,
    scopeEpoch: 0,
    snapshotReady: false,
    polling: false,
    stopped: false,
    announcementTimer: null,
    pendingAnnouncements: [],
    urgentAnnouncementTimer: null,
    pendingUrgentAnnouncements: [],
    announcementGeneration: 0,
    restoreFocusId: null,
    pendingCommand: null,
    pendingCommandHostId: null
  };

  const byId = (id) => document.getElementById(id);
  const RESTORABLE_FOCUS_IDS = new Set([
    "main",
    "permissions-region",
    "permissions-filter",
    "permissions-copy",
    "strategy-region",
    "portfolio-region",
    "operations-region",
    "operations-filter",
    "operations-copy",
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
    "permissions-sort",
    "permissions-previous",
    "permissions-next",
    "strategy-sort",
    "strategy-previous",
    "strategy-next",
    "portfolio-sort",
    "portfolio-previous",
    "portfolio-next",
    "operations-sort",
    "operations-previous",
    "operations-next",
    "risk-sort",
    "risk-previous",
    "risk-next",
    "jobs-sort",
    "jobs-previous",
    "jobs-next",
    "event-history-sort",
    "event-history-previous",
    "event-history-next",
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
    if (
      typeof value !== "string" ||
      !/^(0|[1-9][0-9]*)$/.test(value)
    ) {
      throw new Error(name + " must be a canonical Sequence string");
    }
    return BigInt(value);
  }

  function requiredText(value, name) {
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error(name + " must be a non-empty string");
    }
    return value;
  }

  function canonicalId(value, name) {
    const token = requiredText(value, name);
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(token)) {
      throw new Error(name + " must be a canonical UUID");
    }
    return token;
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

  function compareCanonicalUtcInstants(left, right, leftName, rightName) {
    const leftToken = utcInstant(left, leftName);
    const rightToken = utcInstant(right, rightName);
    const parts = (token) => {
      const withoutZ = token.slice(0, -1);
      const dot = withoutZ.indexOf(".");
      return dot === -1
        ? [withoutZ, ""]
        : [withoutZ.slice(0, dot), withoutZ.slice(dot + 1)];
    };
    const [leftBase, leftFraction] = parts(leftToken);
    const [rightBase, rightFraction] = parts(rightToken);
    if (leftBase < rightBase) return -1;
    if (leftBase > rightBase) return 1;
    const width = Math.max(leftFraction.length, rightFraction.length);
    const normalizedLeft = leftFraction.padEnd(width, "0");
    const normalizedRight = rightFraction.padEnd(width, "0");
    if (normalizedLeft < normalizedRight) return -1;
    if (normalizedLeft > normalizedRight) return 1;
    return 0;
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

  function pendingCommandMatchesCurrentContext() {
    return state.pendingCommand === null || (
      state.pendingCommandHostId === state.renderedHostId &&
      state.sessionIdentity !== null &&
      state.pendingCommand.actor === state.sessionIdentity.actor &&
      state.pendingCommand.session === state.sessionIdentity.session &&
      state.pendingCommand.account_id === state.accountId &&
      state.pendingCommand.environment === state.environment
    );
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
      String(state.renderedHostId ?? ""),
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
    text("authority-policy-host", active ? state.renderedHostId : null);
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
    const confirmation = byId("authority-policy-confirm");
    if (confirmation && (
        confirmation.dataset.reviewCommandId !== state.pendingCommand.command_id ||
        confirmation.dataset.reviewStateVersion !== state.version.toString() ||
        confirmation.dataset.reviewScope !== authorityReviewScopeKey())) {
      invalidateAuthorityPolicyReview();
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
    text("authority-policy-host", state.pendingCommandHostId);
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
    if (!Array.isArray(snapshot.jobs) ||
        snapshot.jobs.some((item) =>
          !item || typeof item !== "object" || Array.isArray(item))) {
      throw new Error("jobs must be an array of objects");
    }

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
    const commandId = canonicalId(result.command_id, "command_id");
    if (commandId !== expectedCommandId) {
      throw new Error("CommandResult command_id does not match the submitted command");
    }
    if (!["ACCEPTED", "REJECTED", "CONFLICT"].includes(result.status)) {
      throw new Error("CommandResult status is not canonical");
    }
    if (!Array.isArray(result.field_errors) ||
        result.field_errors.some((item) =>
          !item || typeof item !== "object" || Array.isArray(item))) {
      throw new Error("field_errors must be an array of objects");
    }
    const operationId = result.operation_id === undefined
      ? null
      : canonicalId(result.operation_id, "operation_id");
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
    const operationId = canonicalId(result.operation_id, "operation_id");
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
    if (compareCanonicalUtcInstants(
        updatedAt,
        startedAt,
        "updated_at",
        "started_at") < 0) {
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
    const pendingContextMatches = pendingCommandMatchesCurrentContext();
    if (button) {
      button.disabled = !enabled || !roleAllowed || !pendingContextMatches;
    }
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
    const response = await fetch(HOST_API.route("submitCommand"), {
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
      let errorBody = null;
      try {
        const contentType = response.headers.get("Content-Type") || "";
        if (contentType.includes("application/json")) {
          errorBody = await response.json();
        }
      } catch {
        errorBody = null;
      }
      const error = new Error("Host command failed with status " + response.status);
      error.status = response.status;
      if (errorBody && typeof errorBody === "object" && !Array.isArray(errorBody) &&
          typeof errorBody.error === "string") {
        error.code = errorBody.error;
      }
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
  function renderCommandValidationDetails(fieldErrors, stateName) {
    const list = byId("command-validation-list");
    if (!list) return;
    list.replaceChildren();

    const appendMessage = (message) => {
      const item = document.createElement("li");
      item.textContent = message;
      list.appendChild(item);
    };

    if (stateName === "pending") {
      appendMessage(
        "No confirmed host field-validation details are available for this command attempt yet.");
      return;
    }
    if (stateName === "unavailable") {
      appendMessage(
        "Field-specific validation details are unavailable because the command response could not be confirmed.");
      return;
    }
    if (stateName === "scope_changed") {
      appendMessage(
        "Field-validation details from the previous account/environment scope are not shown in this scope.");
      return;
    }
    if (stateName !== "confirmed" || !Array.isArray(fieldErrors)) {
      throw new Error("command validation detail state is invalid");
    }
    if (fieldErrors.length === 0) {
      appendMessage("No field-specific validation errors reported by the host.");
      return;
    }
    for (const error of fieldErrors) {
      appendMessage(projectionText(error));
    }
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

  function selectedCellEndpoint(body, node, offset) {
    const element = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
    const cell = element && element.closest ? element.closest("th, td") : null;
    const row = cell && cell.parentElement;
    if (!cell || !row || !body.contains(row) || !row.dataset.selectionKey) return null;
    const cellIndex = [...row.cells].indexOf(cell);
    if (cellIndex < 0) return null;
    const prefix = document.createRange();
    prefix.selectNodeContents(cell);
    try {
      prefix.setEnd(node, offset);
    } catch {
      return null;
    }
    return Object.freeze({
      rowKey: row.dataset.selectionKey,
      rowText: row.dataset.selectionExact === "true" ? row.textContent : null,
      cellIndex,
      textOffset: prefix.toString().length
    });
  }

  function captureTableSelection(body) {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
    const anchor = selectedCellEndpoint(body, selection.anchorNode, selection.anchorOffset);
    const focus = selectedCellEndpoint(body, selection.focusNode, selection.focusOffset);
    return anchor !== null && focus !== null ? Object.freeze({anchor, focus}) : null;
  }

  function textPointAtOffset(cell, requestedOffset) {
    let remaining = Math.max(0, requestedOffset);
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    let last = null;
    while (node !== null) {
      last = node;
      if (remaining <= node.data.length) {
        return Object.freeze({node, offset: remaining});
      }
      remaining -= node.data.length;
      node = walker.nextNode();
    }
    return last === null
      ? null
      : Object.freeze({node: last, offset: last.data.length});
  }

  function rowForSelectionEndpoint(body, endpoint) {
    return [...body.rows].find(
      (candidate) => candidate.dataset.selectionKey === endpoint.rowKey &&
        (endpoint.rowText === null || candidate.textContent === endpoint.rowText)) || null;
  }

  function revealBookmarkedTablePage(body, bookmark) {
    if (bookmark === null) return;
    const tool = toolForBody(body.id);
    const filter = tool === null ? null : byId(tool.filterId);
    if (tool === null || !filter) return;

    const rows = filterableRows(body);
    const anchorRow = rowForSelectionEndpoint(body, bookmark.anchor);
    const focusRow = rowForSelectionEndpoint(body, bookmark.focus);
    if (anchorRow === null || focusRow === null) return;

    ensureTableHostOrder(rows);
    const query = normalizedTableQuery(filter.value);
    const ordered = orderedTableRows(tool, rows, tableSortMode(tool));
    const matching = ordered.filter(
      (row) => query === "" || tableSearchText(row).includes(query));
    const anchorIndex = matching.indexOf(anchorRow);
    const focusIndex = matching.indexOf(focusRow);
    if (anchorIndex < 0 || focusIndex < 0) return;

    const anchorPage = Math.floor(anchorIndex / TABLE_PAGE_SIZE);
    const focusPage = Math.floor(focusIndex / TABLE_PAGE_SIZE);
    if (anchorPage !== focusPage) return;

    const view = tableViewFor(tool);
    if (view.page === anchorPage) return;
    view.page = anchorPage;
    applyTableFilter(tool, {announce: false});
  }

  function restoreTableSelection(body, bookmark) {
    if (bookmark === null) return;
    const findPoint = (endpoint) => {
      const row = rowForSelectionEndpoint(body, endpoint);
      if (!row || row.hidden) return null;
      const cell = row.cells[endpoint.cellIndex];
      return cell ? textPointAtOffset(cell, endpoint.textOffset) : null;
    };
    const anchor = findPoint(bookmark.anchor);
    const focus = findPoint(bookmark.focus);
    if (anchor === null || focus === null) return;
    const selection = window.getSelection();
    if (!selection) return;
    selection.removeAllRanges();
    if (typeof selection.setBaseAndExtent === "function") {
      try {
        selection.setBaseAndExtent(
          anchor.node, anchor.offset, focus.node, focus.offset);
      } catch {
        return;
      }
      return;
    }
    const range = document.createRange();
    try {
      range.setStart(anchor.node, anchor.offset);
      range.setEnd(focus.node, focus.offset);
      if (range.collapsed) {
        range.setStart(focus.node, focus.offset);
        range.setEnd(anchor.node, anchor.offset);
      }
    } catch {
      return;
    }
    selection.addRange(range);
  }

  function preserveTableSelection(body, enabled, render) {
    const bookmark = enabled ? captureTableSelection(body) : null;
    render();
    revealBookmarkedTablePage(body, bookmark);
    restoreTableSelection(body, bookmark);
  }

  
function appendProjectionRow(body, label, value) {
    const row = document.createElement("tr");
    row.dataset.filterableRow = "true";
    row.dataset.tableHostOrder = String(filterableRows(body).length);
    row.dataset.selectionKey = "projection:" + label;
    const header = document.createElement("th");
    header.scope = "row";
    header.textContent = label;
    const cell = document.createElement("td");
    cell.textContent = value;
    row.append(header, cell);
    body.appendChild(row);
    return row;
  }

  function renderProjection(bodyId, record, emptyMessage, {preserveSelection = true} = {}) {
    const body = byId(bodyId);
    if (!body) return;
    preserveTableSelection(body, preserveSelection, () => {
      body.replaceChildren();
      const entries = flattenProjectionRows(record);
      if (entries.length === 0) {
        const row = document.createElement("tr");
        row.dataset.selectionKey = "empty";
        const cell = document.createElement("td");
        cell.colSpan = 2;
        cell.textContent = emptyMessage;
        row.appendChild(cell);
        body.appendChild(row);
      } else {
        for (const [key, value] of entries) {
          appendProjectionRow(body, key, value);
        }
      }
      reapplyTableFilter(bodyId);
    });
  }
  function renderPermissionSummary(permissionSummary, {preserveSelection = true} = {}) {
    const body = byId("permissions-body");
    if (!body) return;
    preserveTableSelection(body, preserveSelection, () => {
      body.replaceChildren();
      appendProjectionRow(body, "Actor", permissionSummary.actor);
      appendProjectionRow(body, "Session", permissionSummary.session);
      appendProjectionRow(body, "Role", permissionSummary.role);
      if (permissionSummary.capabilities.length === 0) {
        appendProjectionRow(
          body, "Capabilities", "No capabilities reported by the host snapshot.");
      } else {
        permissionSummary.capabilities.forEach((capability, index) => {
          const row = appendProjectionRow(
            body, "Capability " + String(index + 1), capability);
          // Capability position is not durable identity; do not retarget a
          // selection if different evidence later occupies the same position.
          row.dataset.selectionExact = "true";
        });
      }
      reapplyTableFilter("permissions-body");
    });
  }
  function renderJobs(jobs, {preserveSelection = true} = {}) {
    const body = byId("jobs-body");
    if (!body) return;
    preserveTableSelection(body, preserveSelection, () => {
      body.replaceChildren();
      if (jobs.length === 0) {
        const row = document.createElement("tr");
        row.dataset.selectionKey = "empty";
        const cell = document.createElement("td");
        cell.colSpan = 2;
        cell.textContent = "No background jobs reported by the host snapshot.";
        row.appendChild(cell);
        body.appendChild(row);
      } else {
        jobs.forEach((job, index) => {
          const row = document.createElement("tr");
          row.dataset.filterableRow = "true";
          row.dataset.tableHostOrder = String(index);
          row.dataset.selectionKey = "job:" + String(index + 1);
          // UiSnapshot.jobs has no required durable job identifier. Position
          // alone is not identity, so preserve a selected row only while its
          // complete rendered evidence remains unchanged.
          row.dataset.selectionExact = "true";
          const header = document.createElement("th");
          header.scope = "row";
          header.textContent = "Job " + String(index + 1);
          const cell = document.createElement("td");
          cell.textContent = projectionText(job);
          row.append(header, cell);
          body.appendChild(row);
        });
      }
      reapplyTableFilter("jobs-body");
    });
  }
  function normalizedTableQuery(value) {
    return String(value ?? "").trim().toLowerCase();
  }

  function tableSearchText(row) {
    return [...row.cells]
      .map((cell) => cell.textContent.replace(/\s+/g, " ").trim())
      .join(" ")
      .toLowerCase();
  }

  function toolForBody(bodyId) {
    return TABLE_TOOLS.find((tool) => tool.bodyId === bodyId) || null;
  }

  function filterableRows(body) {
    return [...body.querySelectorAll('tr[data-filterable-row="true"]')];
  }

  function tableViewFor(tool) {
    let view = tableViewState.get(tool.bodyId);
    if (view === undefined) {
      view = {page: 0};
      tableViewState.set(tool.bodyId, view);
    }
    return view;
  }

  function tableSortMode(tool) {
    const control = byId(tool.sortId);
    const value = control ? control.value : "host";
    return ["host", "text-asc", "text-desc"].includes(value) ? value : "host";
  }

  function ensureTableHostOrder(rows) {
    rows.forEach((row, index) => {
      if (row.dataset.tableHostOrder === undefined) {
        row.dataset.tableHostOrder = String(index);
      }
    });
  }

  function compareHostOrder(tool, left, right) {
    if (tool.bodyId === "event-history-body") {
      const a = BigInt(left.dataset.tableHostOrder);
      const b = BigInt(right.dataset.tableHostOrder);
      return a === b ? 0 : (a > b ? -1 : 1);
    }
    return Number(left.dataset.tableHostOrder) - Number(right.dataset.tableHostOrder);
  }

  function orderedTableRows(tool, rows, mode) {
    const ordered = [...rows];
    ordered.sort((left, right) => {
      if (mode === "host") return compareHostOrder(tool, left, right);
      const a = tableSearchText(left);
      const b = tableSearchText(right);
      const primary = a < b ? -1 : (a > b ? 1 : 0);
      if (primary !== 0) return mode === "text-desc" ? -primary : primary;
      return compareHostOrder(tool, left, right);
    });
    return ordered;
  }

  function tableSortDescription(mode) {
    if (mode === "text-asc") return "rendered text ascending";
    if (mode === "text-desc") return "rendered text descending";
    return "host order";
  }

  function applyTableFilter(tool, {announce = true, resetPage = false} = {}) {
    const body = byId(tool.bodyId);
    const filter = byId(tool.filterId);
    const previous = byId(tool.previousId);
    const next = byId(tool.nextId);
    if (!body || !filter || !previous || !next) return;
    const rows = filterableRows(body);
    const query = normalizedTableQuery(filter.value);
    const view = tableViewFor(tool);
    if (resetPage) view.page = 0;
    ensureTableHostOrder(rows);
    const mode = tableSortMode(tool);
    const ordered = orderedTableRows(tool, rows, mode);
    for (const row of ordered) body.appendChild(row);

    const matching = [];
    for (const row of ordered) {
      const matches = query === "" || tableSearchText(row).includes(query);
      row.hidden = !matches;
      if (matches) matching.push(row);
    }

    const pageCount = Math.max(1, Math.ceil(matching.length / TABLE_PAGE_SIZE));
    view.page = Math.min(Math.max(0, view.page), pageCount - 1);
    const pageStart = view.page * TABLE_PAGE_SIZE;
    const pageEnd = Math.min(pageStart + TABLE_PAGE_SIZE, matching.length);
    matching.forEach((row, index) => {
      if (index < pageStart || index >= pageEnd) row.hidden = true;
    });
    previous.disabled = matching.length === 0 || view.page === 0;
    next.disabled = matching.length === 0 || view.page >= pageCount - 1;

    let statusMessage;
    if (rows.length === 0) {
      statusMessage = "No host rows are available to filter.";
    } else if (matching.length === 0) {
      statusMessage = "0 of " + String(rows.length) +
        " rows match the current filter. Sort: " + tableSortDescription(mode) + ".";
    } else {
      statusMessage =
        "Rows " + String(pageStart + 1) + "-" + String(pageEnd) +
        " of " + String(matching.length) +
        (query === "" ? " rows" : " matching rows") +
        " shown. Page " + String(view.page + 1) + " of " + String(pageCount) +
        ". Sort: " + tableSortDescription(mode) + ".";
    }
    text(tool.statusId, statusMessage);
    if (announce) queuePoliteAnnouncement(statusMessage);
  }

  function reapplyTableFilter(bodyId) {
    const tool = toolForBody(bodyId);
    if (tool !== null) applyTableFilter(tool, {announce: false});
  }

  function resetTableFiltersForScopeChange() {
    for (const tool of TABLE_TOOLS) {
      const filter = byId(tool.filterId);
      const sort = byId(tool.sortId);
      if (filter) filter.value = "";
      if (sort) sort.value = "host";
      tableViewFor(tool).page = 0;
      text(tool.statusId, "Table view reset for new account/environment scope.");
    }
    queuePoliteAnnouncement("Table filters, sort order, and pages reset for new account/environment scope.");
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

  function tabSeparatedTableHeaderText(tool) {
    const body = byId(tool.bodyId);
    const table = body ? body.closest("table") : null;
    if (!table) return "";
    return [...table.querySelectorAll("thead th")]
      .map((cell) => cell.textContent.replace(/\s+/g, " ").trim())
      .join("\t");
  }

  async function copyVisibleTableRows(tool) {
    const scopeEpoch = state.scopeEpoch;
    const rows = visibleTableRows(tool);
    if (rows.length === 0) {
      const message = "No visible " + tool.label + " rows are available to copy.";
      text(tool.statusId, message);
      queuePoliteAnnouncement(message);
      return;
    }
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      const message = "Clipboard access is unavailable. Use normal text selection and copy.";
      text(tool.statusId, message);
      queuePoliteAnnouncement(message);
      return;
    }
    const rowPayload = rows.map((row) => tabSeparatedRowText(row));
    const header = tabSeparatedTableHeaderText(tool);
    const payload = (header === "" ? rowPayload : [header, ...rowPayload]).join("\n");
    try {
      await navigator.clipboard.writeText(payload);
      if (scopeEpoch !== state.scopeEpoch) return;
      const message = String(rows.length) + " visible " + tool.label + " rows copied." +
        (header === "" ? "" : " Column headings included.");
      text(tool.statusId, message);
      queuePoliteAnnouncement(message);
    } catch {
      if (scopeEpoch !== state.scopeEpoch) return;
      const message = "Clipboard copy was not permitted. Use normal text selection and copy.";
      text(tool.statusId, message);
      queuePoliteAnnouncement(message);
    }
  }

  function bindTableTools() {
    for (const tool of TABLE_TOOLS) {
      const filter = byId(tool.filterId);
      const copy = byId(tool.copyId);
      const sort = byId(tool.sortId);
      const previous = byId(tool.previousId);
      const next = byId(tool.nextId);
      if (!filter || !copy || !sort || !previous || !next) continue;
      filter.addEventListener("input", () =>
        applyTableFilter(tool, {resetPage: true}));
      sort.addEventListener("change", () =>
        applyTableFilter(tool, {resetPage: true}));
      previous.addEventListener("click", () => {
        const view = tableViewFor(tool);
        view.page = Math.max(0, view.page - 1);
        applyTableFilter(tool);
      });
      next.addEventListener("click", () => {
        const view = tableViewFor(tool);
        view.page += 1;
        applyTableFilter(tool);
      });
      copy.addEventListener("click", () => { void copyVisibleTableRows(tool); });
      applyTableFilter(tool, {announce: false});
    }
  }

  function announceLiveText(id, message) {
    const element = byId(id);
    if (!element) return;
    // Bind deferred speech to the evidence generation that scheduled it.
    // A host/account/environment reset must never speak old-context text.
    const generation = state.announcementGeneration;
    element.textContent = "";
    window.setTimeout(() => {
      if (generation === state.announcementGeneration) {
        element.textContent = message;
      }
    }, 0);
  }

  function discardQueuedAnnouncementsForEvidenceReset() {
    state.announcementGeneration += 1;
    if (state.announcementTimer !== null) {
      window.clearTimeout(state.announcementTimer);
      state.announcementTimer = null;
    }
    if (state.urgentAnnouncementTimer !== null) {
      window.clearTimeout(state.urgentAnnouncementTimer);
      state.urgentAnnouncementTimer = null;
    }
    state.pendingAnnouncements = [];
    state.pendingUrgentAnnouncements = [];
    text("polite-status", "");
    text("urgent-status", "");
  }

  function queuePoliteAnnouncement(message) {
    if (!message) return;
    state.pendingAnnouncements.push(message);
    if (state.announcementTimer !== null) return;
    state.announcementTimer = window.setTimeout(() => {
      const pending = state.pendingAnnouncements;
      state.pendingAnnouncements = [];
      state.announcementTimer = null;
      // Preserve repeated independent feedback while using one polite live region.
      announceLiveText("polite-status", pending.join(" "));
    }, 750);
  }

  function announce(message, urgent = false, historyKey = null) {
    if (!message) return;
    const history = byId("notification-history");
    const normalizedHistoryKey = historyKey === null ? null : String(historyKey);
    if (history && normalizedHistoryKey !== null &&
        [...history.children].some(
          (item) => item.dataset.notificationKey === normalizedHistoryKey)) {
      return;
    }
    if (history && history.children.length === 1 &&
        history.firstElementChild.textContent.startsWith("No material")) {
      history.replaceChildren();
    }
    if (history) {
      const item = document.createElement("li");
      item.textContent = message;
      if (normalizedHistoryKey !== null) {
        item.dataset.notificationKey = normalizedHistoryKey;
      }
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
        announceLiveText("urgent-status", pending.join(" "));
      }, 0);
      return;
    }
    queuePoliteAnnouncement(message);
  }

  function invalidateSnapshotAuthority() {
    state.snapshotReady = false;
    state.sessionIdentity = null;
    state.accountId = null;
    state.environment = null;
    setCommandAvailability(false);
  }

  function isSnapshotBusy(error) {
    return error !== null && typeof error === "object" &&
      error.status === 503 &&
      error.code === "SNAPSHOT_BUSY" &&
      error.retryable === true;
  }

  function isCommandAuthRejection(error) {
    return error !== null && typeof error === "object" &&
      error.status === 403 &&
      error.code === "AUTHENTICATION_OR_AUTHORIZATION_FAILED";
  }


  function reportSnapshotBusy() {
    invalidateSnapshotAuthority();
    const message =
      "Host snapshot is temporarily busy while durable state changes; waiting for one coherent snapshot. " +
      "Commands remain blocked and displayed values may be stale.";
    text("freshness", message);
    queuePoliteAnnouncement(message);
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
      let errorBody = null;
      try {
        const contentType = response.headers.get("Content-Type") || "";
        if (contentType.includes("application/json")) {
          errorBody = await response.json();
        }
      } catch {
        errorBody = null;
      }
      const error = new Error(`Host request failed with status ${response.status}`);
      error.status = response.status;
      if (errorBody && typeof errorBody === "object" && !Array.isArray(errorBody)) {
        if (typeof errorBody.error === "string") error.code = errorBody.error;
        if (errorBody.retryable === true) error.retryable = true;
      }
      throw error;
    }
    return response.json();
  }

  
function renderOperation(operation) {
    const body = byId("operations-body");
    if (!body) return;
    const bookmark = captureTableSelection(body);

    let row = [...body.querySelectorAll("tr")].find(
      (item) => item.dataset.operationId === operation.operationId);
    if (!row) {
      if (body.children.length === 1 &&
          body.firstElementChild.dataset.operationId === undefined) {
        body.replaceChildren();
      }
      row = document.createElement("tr");
      row.dataset.operationId = operation.operationId;
      row.dataset.tableHostOrder = String(filterableRows(body).length);
      const rowHeader = document.createElement("th");
      rowHeader.scope = "row";
      row.appendChild(rowHeader);
      for (let index = 1; index < 4; index += 1) {
        row.appendChild(document.createElement("td"));
      }
      body.appendChild(row);
    }

    row.dataset.filterableRow = "true";
    row.dataset.selectionKey = "operation:" + operation.operationId;
    row.dataset.selectionExact = "true";
    row.children[0].textContent = operation.operationId;
    row.children[1].textContent = operation.phase;
    row.children[2].textContent = operation.updatedAt;
    row.children[3].textContent = operation.remainingUncertainty.length > 0
      ? operation.remainingUncertainty.join(", ")
      : "None reported";
    reapplyTableFilter("operations-body");
    revealBookmarkedTablePage(body, bookmark);
    restoreTableSelection(body, bookmark);
  }

  async function refreshOperation(operationId) {
    const scopeEpoch = state.scopeEpoch;
    const renderedHostId = state.renderedHostId;
    const renderedAccountId = state.renderedAccountId;
    const renderedEnvironment = state.renderedEnvironment;
    let raw;
    try {
      raw = await jsonFetch(
        HOST_API.route("getOperation", {operation_id: operationId}));
    } catch (error) {
      if (
        scopeEpoch !== state.scopeEpoch ||
        renderedHostId !== state.renderedHostId ||
        renderedAccountId !== state.renderedAccountId ||
        renderedEnvironment !== state.renderedEnvironment
      ) {
        return null;
      }
      throw error;
    }
    const operation = parseOperationResult(raw, operationId);
    if (
      scopeEpoch !== state.scopeEpoch ||
      renderedHostId !== state.renderedHostId ||
      renderedAccountId !== state.renderedAccountId ||
      renderedEnvironment !== state.renderedEnvironment
    ) {
      return null;
    }
    renderOperation(operation);
    return operation;
  }

  function renderHostEvent(event, cursor, stateVersion) {
    const body = byId("event-history-body");
    if (!body) return;
    const bookmark = captureTableSelection(body);
    const kind = requiredText(
      event.kind ?? event.event_type,
      "event.kind");
    const payload = event.payload === undefined ? {} : event.payload;
    const cursorText = cursor.toString();
    const stateVersionText = stateVersion.toString();
    const payloadText = projectionText(payload);

    if (body.children.length === 1 &&
        body.firstElementChild.dataset.hostEventCursor === undefined) {
      body.replaceChildren();
    }

    const matchingRows = [...body.querySelectorAll("tr")].filter(
      (candidate) => candidate.dataset.hostEventCursor === cursorText);
    if (matchingRows.length > 1) {
      throw new Error("received host-event history contains a duplicate cursor");
    }
    let row = matchingRows.length === 1 ? matchingRows[0] : null;
    const expectedCells = [cursorText, stateVersionText, kind, payloadText];
    if (row !== null) {
      const renderedCells = [...row.cells].map((cell) => cell.textContent);
      if (
        row.dataset.tableHostOrder !== cursorText ||
        row.dataset.selectionKey !== "event:" + cursorText ||
        row.dataset.selectionExact !== "true" ||
        renderedCells.length !== expectedCells.length ||
        renderedCells.some((value, index) => value !== expectedCells[index])
      ) {
        throw new Error("host event cursor was reused with conflicting rendered content");
      }
    } else {
      row = document.createElement("tr");
      row.dataset.hostEventCursor = cursorText;
      row.dataset.filterableRow = "true";
      row.dataset.tableHostOrder = cursorText;
      row.dataset.selectionKey = "event:" + cursorText;
      row.dataset.selectionExact = "true";
      const rowHeader = document.createElement("th");
      rowHeader.scope = "row";
      row.appendChild(rowHeader);
      for (let index = 1; index < 4; index += 1) {
        row.appendChild(document.createElement("td"));
      }
      row.children[0].textContent = cursorText;
      row.children[1].textContent = stateVersionText;
      row.children[2].textContent = kind;
      row.children[3].textContent = payloadText;
      body.prepend(row);
    }

    const retained = filterableRows(body)
      .filter((candidate) => candidate.dataset.hostEventCursor !== undefined)
      .sort((left, right) => {
        const a = BigInt(left.dataset.hostEventCursor);
        const b = BigInt(right.dataset.hostEventCursor);
        return a === b ? 0 : (a > b ? -1 : 1);
      });
    for (const expired of retained.slice(100)) expired.remove();
    reapplyTableFilter("event-history-body");
    revealBookmarkedTablePage(body, bookmark);
    restoreTableSelection(body, bookmark);
  }

  function resetNotificationsForScope(
    emptyMessage = "No material notifications recorded in this account/environment session."
  ) {
    if (state.announcementTimer !== null) {
      window.clearTimeout(state.announcementTimer);
      state.announcementTimer = null;
    }
    if (state.urgentAnnouncementTimer !== null) {
      window.clearTimeout(state.urgentAnnouncementTimer);
      state.urgentAnnouncementTimer = null;
    }
    state.pendingAnnouncements = [];
    state.pendingUrgentAnnouncements = [];
    text("polite-status", "", "");
    text("urgent-status", "", "");

    const history = byId("notification-history");
    if (!history) return;
    history.replaceChildren();
    const item = document.createElement("li");
    item.textContent = emptyMessage;
    history.appendChild(item);
  }

  function resetCommandFeedbackForContext(
    hostId,
    accountId,
    environment,
    sessionIdentity
  ) {
    if (state.pendingCommand !== null) {
      const belongsToNewContext =
        state.pendingCommandHostId === hostId &&
        sessionIdentity !== null &&
        state.pendingCommand.actor === sessionIdentity.actor &&
        state.pendingCommand.session === sessionIdentity.session &&
        state.pendingCommand.account_id === accountId &&
        state.pendingCommand.environment === environment;
      text(
        "command-result",
        belongsToNewContext
          ? "An unresolved command for this exact authenticated host/account/environment context is retained with its original identity. Review current host state before exact retry."
          : "An unresolved command from a different host/session/account/environment context is retained with its original identity and will not be retargeted.");
    } else {
      text(
        "command-result",
        "No host command has been submitted for this host/account/environment session.");
    }
    renderCommandValidationDetails([], "scope_changed");
  }

  function resetOperationsForScope(
    emptyMessage = "No host operations loaded for this account/environment session."
  ) {
    const body = byId("operations-body");
    if (!body) return;
    body.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.textContent = emptyMessage;
    row.appendChild(cell);
    body.appendChild(row);
    reapplyTableFilter("operations-body");
  }

  function resetEventHistoryForScope(
    emptyMessage = "No canonical host events received in this account/environment session."
  ) {
    const body = byId("event-history-body");
    if (!body) return;
    body.replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.textContent = emptyMessage;
    row.appendChild(cell);
    body.appendChild(row);
    reapplyTableFilter("event-history-body");
  }

  function renderSnapshot(snapshot, {announceRefresh = false} = {}) {
    const parsed = parseCanonicalSnapshot(snapshot);
    // Command authority is intentionally cleared on trust loss, but the last
    // successfully rendered scope is retained only to prevent stale read-only
    // evidence from crossing into a later account/environment view.
    const hostChanged =
      state.renderedHostId !== null &&
      parsed.hostId !== state.renderedHostId;
    const scopeChanged = state.renderedAccountId !== null && (
      parsed.accountId !== state.renderedAccountId ||
      parsed.environment !== state.renderedEnvironment);
    const displayContextChanged = hostChanged || scopeChanged;
    const priorCursor = state.cursor;
    const skippedSameScopeEvents =
      !displayContextChanged &&
      state.renderedHostId !== null &&
      parsed.cursor > priorCursor;
    if (displayContextChanged) {
      state.scopeEpoch += 1;
      state.cursor = 0n;
      state.version = 0n;
      discardQueuedAnnouncementsForEvidenceReset();
      resetNotificationsForScope();
      resetTableFiltersForScopeChange();
      resetOperationsForScope();
      resetEventHistoryForScope();
      resetCommandFeedbackForContext(
        parsed.hostId,
        parsed.accountId,
        parsed.environment,
        parsed.sessionIdentity);
    }
    if (parsed.version < state.version || parsed.cursor < state.cursor) {
      throw new Error("host snapshot counters regressed");
    }
    if (skippedSameScopeEvents) {
      const gap =
        "Canonical snapshot advanced from event cursor " + priorCursor.toString() +
        " to " + parsed.cursor.toString() +
        " before those host events were received by this page.";
      discardQueuedAnnouncementsForEvidenceReset();
      resetNotificationsForScope(
        "Notification history was cleared because " + gap);
      resetOperationsForScope(
        "Current host operations were cleared because " + gap);
      resetEventHistoryForScope(
        "Received host-event history was cleared because " + gap);
      announce(
        gap +
          " Event-derived operation, notification, and received-event views were cleared rather than shown as current.",
        true);
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
    text("provider-availability", "UNAVAILABLE — the current UiSnapshot exposes no provider-capability authority. Host-supported ZERO/SIMULATION/research workflows do not require provider setup.");
    text(
      "connection-summary",
      "Host: " + parsed.hostId + ". Account: " + parsed.accountId +
        ". Environment: " + parsed.environment + ".");
    text("freshness", freshnessText(parsed));
    text("server-time", parsed.serverTime);
    renderPermissionSummary(parsed.permissionSummary, {preserveSelection: !displayContextChanged});
    renderProjection(
      "portfolio-body",
      parsed.portfolio,
      "No portfolio projection reported by the host snapshot.",
      {preserveSelection: !displayContextChanged});
    renderProjection(
      "risk-body",
      parsed.risk,
      "No risk projection reported by the host snapshot.",
      {preserveSelection: !displayContextChanged});
    renderProjection(
      "strategy-body",
      parsed.strategy,
      "No strategy or decision projection reported by the host snapshot.",
      {preserveSelection: !displayContextChanged});
    renderJobs(parsed.jobs, {preserveSelection: !displayContextChanged});
    state.renderedHostId = parsed.hostId;
    state.renderedAccountId = parsed.accountId;
    state.renderedEnvironment = parsed.environment;
    if (displayContextChanged) {
      announce(
        "Host display context changed to host " + parsed.hostId +
          ", account " + parsed.accountId +
          " in " + parsed.environment +
          ". Old-context operation, event, notification, and command-validation evidence was cleared.",
        parsed.environment === "LIVE" || hostChanged);
    }

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
    state.scopeEpoch += 1;
    const refreshEpoch = state.scopeEpoch;
    let snapshot;
    try {
      snapshot = await jsonFetch(HOST_API.route("getState"));
    } catch (error) {
      if (refreshEpoch !== state.scopeEpoch) {
        return false;
      }
      throw error;
    }
    if (refreshEpoch !== state.scopeEpoch) {
      return false;
    }
    renderSnapshot(snapshot, options);
    return true;
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
    let pollEpoch = null;
    let pollRenderedHostId = null;
    let pollRenderedAccountId = null;
    let pollRenderedEnvironment = null;
    try {
      // A failed post-event snapshot refresh disables commands but must not
      // require a new event or manual action to recover. Re-establish the
      // canonical snapshot before the next event request whenever local state
      // is marked unready.
      if (!state.snapshotReady) {
        const recovered = await refreshSnapshot();
        if (!recovered) {
          return;
        }
      }
      pollEpoch = state.scopeEpoch;
      pollRenderedHostId = state.renderedHostId;
      pollRenderedAccountId = state.renderedAccountId;
      pollRenderedEnvironment = state.renderedEnvironment;
      const response = await jsonFetch(
        HOST_API.route("streamEvents") + "?after=" +
          encodeURIComponent(state.cursor.toString()));
      if (
        pollEpoch !== state.scopeEpoch ||
        pollRenderedHostId !== state.renderedHostId ||
        pollRenderedAccountId !== state.renderedAccountId ||
        pollRenderedEnvironment !== state.renderedEnvironment
      ) {
        return;
      }
      if (!Array.isArray(response)) {
        throw new Error("Host event response must be a canonical JSON array");
      }
      const events = response;
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
          const operationId = canonicalId(
            payload.operation_id,
            "event.payload.operation_id");
          const operation = await refreshOperation(operationId);
          if (operation === null) {
            return;
          }
        }
        if (MATERIAL_EVENTS.has(kind)) {
          announce(
            eventMessage(event),
            URGENT_EVENTS.has(kind),
            "event:" + cursor.toString());
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
      if (
        pollEpoch !== null &&
        (
          pollEpoch !== state.scopeEpoch ||
          pollRenderedHostId !== state.renderedHostId ||
          pollRenderedAccountId !== state.renderedAccountId ||
          pollRenderedEnvironment !== state.renderedEnvironment
        )
      ) {
        return;
      }
      if (error.status === 409 || error.status === 410) {
        try {
          await refreshSnapshot({announceRefresh: true});
        } catch (recoveryError) {
          if (isSnapshotBusy(recoveryError)) {
            reportSnapshotBusy();
          } else {
            invalidateSnapshotAuthority();
            text(
              "freshness",
              "Host synchronization gap could not be recovered; displayed values may be stale.");
            announce(
              "Host synchronization gap recovery failed. Commands remain blocked until a fresh canonical snapshot is available.",
              true);
          }
        }
      } else {
        invalidateSnapshotAuthority();
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
    const actions = raw.split(",");
    if (actions.some((item) => item === "" || item !== item.trim())) {
      throw new Error("allowed actions must be comma-separated canonical names without surrounding whitespace or empty entries");
    }
    if (new Set(actions).size !== actions.length) {
      throw new Error("allowed actions must be unique");
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
    if (compareCanonicalUtcInstants(
        expiresAt,
        validFrom,
        "expires at",
        "valid from") <= 0) {
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
    state.pendingCommandHostId = state.renderedHostId;
    return payload;
  }

  function clearConfirmedCommand(payload) {
    if (state.pendingCommand === payload) {
      state.pendingCommand = null;
      state.pendingCommandHostId = null;
    }
  }

  function commandContextMatchesCurrentSnapshot(payload, submittedHostId) {
    return state.snapshotReady &&
      state.sessionIdentity !== null &&
      submittedHostId === state.renderedHostId &&
      payload.actor === state.sessionIdentity.actor &&
      payload.session === state.sessionIdentity.session &&
      payload.account_id === state.accountId &&
      payload.environment === state.environment;
  }

  async function submitCommand(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    renderCommandValidationDetails([], "pending");

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
        state.pendingCommandHostId !== state.renderedHostId ||
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
    if (recovering && !actionCanSubmitInCurrentScope(
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
      renderCommandValidationDetails([], "pending");
      byId("command-result").focus();
      setCommandAvailability(state.snapshotReady && state.sessionIdentity !== null);
      return;
    }
    // Freeze the visible action/policy controls immediately after the exact
    // command payload is retained. The operator must never see editable values
    // that differ from the in-flight request.
    syncHostActionOptions(state.sessionIdentity.role);
    renderPendingAuthorityPolicyForRetry();
    const submittedHostId = state.pendingCommandHostId;
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
    byId("command-result").focus();
    try {
      const result = await submitCanonicalCommand(payload);
      const responseScopeCurrent = commandContextMatchesCurrentSnapshot(payload, submittedHostId);
      clearConfirmedCommand(payload);
      if (!responseScopeCurrent) {
        renderCommandValidationDetails([], "unavailable");
        const acceptanceCaveat = result.status === "ACCEPTED"
          ? " Acceptance is not a completed financial outcome."
          : "";
        text(
          "command-result",
          "Confirmed host response " + result.status + " for command " + commandId +
            " belongs to the original account/environment scope " +
            payload.account_id + " / " + payload.environment +
            ", but the authenticated host scope changed before the response was displayed." +
            acceptanceCaveat +
            " Operation and field-validation details from the original scope were not rendered into the current scope.");
        byId("command-result").focus();
        return;
      }
      renderCommandValidationDetails(result.fieldErrors, "confirmed");
      if (result.status === "ACCEPTED") {
        let acceptedMessage =
          "Command " + commandId +
          " was accepted for processing. It is not yet a completed financial outcome.";
        if (result.operationId !== null) {
          try {
            const operation = await refreshOperation(result.operationId);
            if (operation === null) {
              text(
                "command-result",
                acceptedMessage +
                  " Operation status was not rendered because the account/environment scope changed while it was loading. " +
                  "This accepted response belongs to the original scope " +
                  payload.account_id + " / " + payload.environment + ".");
              byId("command-result").focus();
              return;
            } else {
              const uncertainty = operation.remainingUncertainty.length > 0
                ? " Remaining uncertainty: " +
                  operation.remainingUncertainty.join(", ") + "."
                : "";
              acceptedMessage +=
                " Operation phase is " + operation.phase + "." + uncertainty;
            }
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
        state.scopeEpoch += 1;
        state.snapshotReady = false;
        state.sessionIdentity = null;
        state.accountId = null;
        state.environment = null;
        setCommandAvailability(false);
        announce(
          "Host state refresh failed after the command response. The confirmed command response remains unchanged.",
          true);
      }
    } catch (error) {
      if (!recovering && isCommandAuthRejection(error)) {
        clearConfirmedCommand(payload);
        invalidateSnapshotAuthority();
        text("command-result",
          "Command " + commandId +
            " was not accepted because the authenticated host session was rejected before command acceptance. Its fresh command identity was discarded; re-establish a valid session and canonical snapshot before trying again.");
      } else if (isSnapshotBusy(error) && commandContextMatchesCurrentSnapshot(payload, submittedHostId)) {
        invalidateSnapshotAuthority();
        reportSnapshotBusy();
        text("command-result",
          "Command " + commandId +
            " could not be confirmed while the host snapshot was temporarily busy. Its original command identity remains retained for exact retry.");
      } else if (commandContextMatchesCurrentSnapshot(payload, submittedHostId)) {
        invalidateSnapshotAuthority();
        text("command-result",
          "Command " + commandId +
            " could not be confirmed. Its original command_id and idempotency_key are retained for exact retry after host state recovers. No durable financial or safety outcome is being claimed.");
      } else {
        text("command-result",
          "Command " + commandId + " from original scope " +
            payload.account_id + " / " + payload.environment +
            " could not be confirmed after the authenticated host scope changed. " +
            "Its original command_id and idempotency_key are retained and will not be retargeted. " +
            "The current scope snapshot is not invalidated by this older request.");
      }
      renderCommandValidationDetails([], "unavailable");
      byId("command-result").focus();
    } finally {
      setCommandAvailability(state.snapshotReady && state.sessionIdentity !== null);
    }
  }

  async function refreshStateFromUser() {
    const button = byId("refresh-state");
    const restoreKeyboardFocus = button !== null && document.activeElement === button;
    if (button) button.disabled = true;
    try {
      await refreshSnapshot();
      announce("Host state refreshed from the canonical snapshot.");
    } catch (error) {
      if (isSnapshotBusy(error)) {
        reportSnapshotBusy();
      } else {
        invalidateSnapshotAuthority();
        text("freshness", "Host unavailable; displayed values may be stale.");
        announce("Host state refresh failed. Displayed values may be stale.", true);
      }
    } finally {
      if (button) {
        button.disabled = false;
        const active = document.activeElement;
        if (restoreKeyboardFocus && (
            active === button ||
            active === document.body ||
            active === document.documentElement)) {
          button.focus();
        }
      }
    }
  }
  // The ten canonical page locations are projections of this document, never
  // browser-held financial, provider or account authority.
  const PAGE_ROUTES = Object.freeze([
    "overview", "accounts", "opportunities", "portfolio", "risk",
    "research", "learning", "models", "history", "settings"
  ]);

  function bindPageNavigation() {
    const nav = document.querySelector('nav[aria-label="Primary"]');
    if (!nav) throw new Error("Canonical primary navigation is missing");
    const pages = new Map();
    for (const id of PAGE_ROUTES) {
      const link = nav.querySelector('a[href="#' + id + '"]');
      const heading = byId(id + "-heading");
      const section = byId(id);
      if (!link || !heading || !section || !section.contains(heading)) {
        throw new Error("Canonical page navigation is incomplete: " + id);
      }
      pages.set(id, {link, heading});
    }
    function activate({focusHeading = false} = {}) {
      // No URL decoding, HTML interpretation, route substitution or Host API call.
      const hash = window.location.hash;
      const id = hash === "" ? "overview" : hash.slice(1);
      const target = hash === "#main" ? null : pages.get(id);
      for (const page of pages.values()) page.link.removeAttribute("aria-current");
      if (hash === "#main") {
        text("page-navigation-status", "Main content. Use headings to move among AutoTrade sections.");
        if (focusHeading) byId("main").focus({preventScroll: true});
        return;
      }
      if (!target || (hash !== "" && hash !== "#" + id)) {
        text("page-navigation-status", "Unknown section. Select a valid page from Primary navigation. No command or provider request was issued.");
        return;
      }
      target.link.setAttribute("aria-current", "location");
      text("page-navigation-status", "Current section: " + target.heading.textContent.trim() + ". Use browser Back and Forward to revisit sections.");
      if (focusHeading) target.heading.focus({preventScroll: true});
    }
    // Ordinary anchors own browser history, URLs, and scroll. This adds only
    // heading focus and an announced active-section state for keyboard/NVDA.
    nav.addEventListener("click", (event) => {
      const link = event.target.closest("a[href]");
      if (!link || !nav.contains(link)) return;
      if (link.getAttribute("href") === window.location.hash) {
        const page = pages.get(window.location.hash.slice(1));
        if (page) page.heading.focus({preventScroll: true});
      }
    });
    window.addEventListener("hashchange", () => activate({focusHeading: true}));
    activate({focusHeading: window.location.hash !== ""});
  }

  async function start() {
    bindPageNavigation();
    bindTableTools();
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
    } catch (error) {
      if (isSnapshotBusy(error)) {
        reportSnapshotBusy();
      } else {
        invalidateSnapshotAuthority();
        text("freshness", "Host unavailable; no current state has been confirmed.");
        announce("Host unavailable. No current state has been confirmed.", true);
      }
    }
    window.setInterval(pollEvents, 2000);
  }

  window.addEventListener("pagehide", () => {
    captureFocusForRestoration();
    state.stopped = true;
    state.scopeEpoch += 1;
    state.snapshotReady = false;
    state.sessionIdentity = null;
    state.accountId = null;
    state.environment = null;
    setCommandAvailability(false);
  });

  window.addEventListener("pageshow", async (event) => {
    if (!event.persisted) return;
    state.stopped = false;
    state.scopeEpoch += 1;
    state.snapshotReady = false;
    state.sessionIdentity = null;
    state.accountId = null;
    state.environment = null;
    setCommandAvailability(false);
    try {
      await refreshSnapshot();
      announce("Host state refreshed after page restoration.");
    } catch (error) {
      if (isSnapshotBusy(error)) {
        reportSnapshotBusy();
      } else {
        invalidateSnapshotAuthority();
        text("freshness", "Host unavailable after page restoration; displayed values may be stale.");
        announce(
          "Host synchronization failed after page restoration. Commands remain blocked.",
          true);
      }
    } finally {
      restoreFocusAfterPageRestore();
    }
  });

  document.addEventListener("DOMContentLoaded", start);
})();
