/**
 * Semantic UI projection for AutoTrade host contracts.
 *
 * No financial logic lives here. This layer renders truthful host state:
 * command acceptance is not completion, UNKNOWN remains uncertainty, stale
 * snapshots stay visibly stale, and an event-cursor gap requires replacement
 * from a fresh snapshot rather than speculative local repair.
 */

export const OPERATION_PHASES = Object.freeze([
  "QUEUED",
  "RUNNING",
  "WAITING_EXTERNAL",
  "SUCCEEDED",
  "FAILED",
  "UNKNOWN",
  "CANCELLED",
]);

export const TERMINAL_PHASES = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);

export class UiContractError extends Error {}

function requiredText(value, name) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new UiContractError(`${name} must be non-empty text`);
  }
  return value.trim();
}

function sequence(value, name) {
  const text = requiredText(String(value), name);
  if (!/^\d+$/.test(text)) {
    throw new UiContractError(`${name} must be a non-negative sequence`);
  }
  return BigInt(text);
}

function stringArray(value, name) {
  if (!Array.isArray(value)) {
    throw new UiContractError(`${name} must be an array`);
  }
  const result = value.map((item, index) =>
    requiredText(item, `${name}[${index}]`),
  );
  if (new Set(result).size !== result.length) {
    throw new UiContractError(`${name} cannot contain duplicates`);
  }
  return Object.freeze(result);
}

export function normalizeSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    throw new UiContractError("snapshot must be an object");
  }
  const stateVersion = sequence(snapshot.state_version, "state_version");
  const eventCursor = sequence(snapshot.event_cursor, "event_cursor");
  if (!snapshot.operations || typeof snapshot.operations !== "object" || Array.isArray(snapshot.operations)) {
    throw new UiContractError("operations must be an object");
  }
  const operations = {};
  for (const [operationId, phaseRaw] of Object.entries(snapshot.operations)) {
    const id = requiredText(operationId, "operation_id");
    const phase = requiredText(phaseRaw, "operation phase");
    if (!OPERATION_PHASES.includes(phase)) {
      throw new UiContractError(`unsupported operation phase: ${phase}`);
    }
    operations[id] = Object.freeze({
      operationId: id,
      phase,
      remainingUncertainty:
        phase === "UNKNOWN"
          ? Object.freeze(["details require operation evidence"])
          : Object.freeze([]),
    });
  }
  return Object.freeze({
    stateVersion,
    eventCursor,
    stale: false,
    staleReason: null,
    operations: Object.freeze(operations),
  });
}

export function applyHostEvent(model, event) {
  if (!model || typeof model !== "object") {
    throw new UiContractError("model is required");
  }
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    throw new UiContractError("event must be an object");
  }

  const cursor = sequence(event.cursor, "cursor");
  const stateVersion = sequence(event.state_version, "state_version");
  const expectedCursor = model.eventCursor + 1n;
  if (cursor !== expectedCursor) {
    return markCursorGap(model, cursor);
  }
  if (stateVersion < model.stateVersion) {
    throw new UiContractError("event state version cannot move backwards");
  }

  const kind = requiredText(event.kind, "event kind");
  const payload = event.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new UiContractError("event payload must be an object");
  }

  const nextOperations = { ...model.operations };
  let announcement = null;

  if (kind === "COMMAND_ACCEPTED") {
    const operationId = requiredText(payload.operation_id, "operation_id");
    if (nextOperations[operationId]) {
      throw new UiContractError("duplicate operation identity");
    }
    const phase = requiredText(payload.phase ?? "QUEUED", "phase");
    if (phase !== "QUEUED") {
      throw new UiContractError("accepted command must begin QUEUED");
    }
    nextOperations[operationId] = Object.freeze({
      operationId,
      phase,
      remainingUncertainty: stringArray(
        payload.remaining_uncertainty ?? ["financial_outcome_not_completed"],
        "remaining_uncertainty",
      ),
    });
    announcement = Object.freeze({
      politeness: "polite",
      key: `operation:${operationId}:QUEUED`,
      text: "Command accepted. Financial outcome is not completed.",
    });
  } else if (kind === "OPERATION_UPDATED") {
    const operationId = requiredText(payload.operation_id, "operation_id");
    const previous = nextOperations[operationId];
    if (!previous) {
      throw new UiContractError("operation update has no accepted operation");
    }
    const phase = requiredText(payload.phase, "phase");
    if (!OPERATION_PHASES.includes(phase) || phase === "QUEUED") {
      throw new UiContractError("unsupported operation update phase");
    }
    if (TERMINAL_PHASES.has(previous.phase)) {
      throw new UiContractError("terminal operation cannot transition");
    }
    if (previous.phase === "UNKNOWN" && !TERMINAL_PHASES.has(phase)) {
      throw new UiContractError("UNKNOWN can only resolve terminally");
    }
    const uncertainty = stringArray(
      payload.remaining_uncertainty ?? [],
      "remaining_uncertainty",
    );
    if (phase === "UNKNOWN" && uncertainty.length === 0) {
      throw new UiContractError("UNKNOWN must retain explicit uncertainty");
    }
    if (TERMINAL_PHASES.has(phase) && uncertainty.length !== 0) {
      throw new UiContractError("terminal phase cannot retain uncertainty");
    }
    nextOperations[operationId] = Object.freeze({
      operationId,
      phase,
      remainingUncertainty: uncertainty,
    });
    announcement = operationAnnouncement(operationId, phase, uncertainty);
  } else {
    // Unknown future event types advance the cursor without inventing UI state.
    announcement = Object.freeze({
      politeness: "polite",
      key: `host-event:${kind}`,
      text: "Host state changed. Details are available in diagnostics.",
    });
  }

  return Object.freeze({
    model: Object.freeze({
      stateVersion,
      eventCursor: cursor,
      stale: false,
      staleReason: null,
      operations: Object.freeze(nextOperations),
    }),
    announcement,
    needsSnapshot: false,
  });
}

export function markCursorGap(model, receivedCursor = null) {
  const suffix =
    receivedCursor === null
      ? ""
      : ` Received cursor ${receivedCursor.toString()}.`;
  return Object.freeze({
    model: Object.freeze({
      ...model,
      stale: true,
      staleReason: "event_cursor_gap",
    }),
    announcement: Object.freeze({
      politeness: "polite",
      key: "host:event-cursor-gap",
      text:
        "Live updates were interrupted. Values are stale until a fresh snapshot is loaded." +
        suffix,
    }),
    needsSnapshot: true,
  });
}

export function operationAnnouncement(operationId, phase, uncertainty = []) {
  const id = requiredText(operationId, "operation_id");
  if (!OPERATION_PHASES.includes(phase)) {
    throw new UiContractError("unsupported operation phase");
  }
  const unresolved = Array.isArray(uncertainty) ? uncertainty.length : 0;
  const terminal = TERMINAL_PHASES.has(phase);

  let text;
  if (phase === "UNKNOWN") {
    text =
      "Operation result is unknown. New exposure must not be inferred from this status.";
  } else if (phase === "SUCCEEDED") {
    text = "Operation completed according to host evidence.";
  } else if (phase === "FAILED") {
    text = "Operation failed. Review the recorded reason and evidence.";
  } else if (phase === "CANCELLED") {
    text = "Operation was cancelled. Verify any already completed financial effects.";
  } else if (phase === "WAITING_EXTERNAL") {
    text = "Operation is waiting for external evidence.";
  } else if (phase === "RUNNING") {
    text = "Operation is running.";
  } else {
    text = "Operation is queued. Financial completion has not occurred.";
  }

  if (terminal && unresolved > 0) {
    throw new UiContractError("terminal operation cannot announce unresolved uncertainty");
  }
  return Object.freeze({
    politeness: "polite",
    key: `operation:${id}:${phase}`,
    text,
  });
}

export class NotificationQueue {
  #items = [];
  #keys = new Set();

  enqueue(notification) {
    if (!notification) return false;
    const key = requiredText(notification.key, "notification key");
    const politeness = requiredText(notification.politeness, "politeness");
    if (!["polite", "assertive"].includes(politeness)) {
      throw new UiContractError("unsupported live-region politeness");
    }
    const text = requiredText(notification.text, "notification text");
    if (this.#keys.has(key)) return false;
    this.#keys.add(key);
    this.#items.push(Object.freeze({ key, politeness, text }));
    return true;
  }

  drain() {
    const items = Object.freeze([...this.#items]);
    this.#items = [];
    this.#keys.clear();
    return items;
  }
}

export function semanticShellDescriptor(model) {
  if (!model || typeof model !== "object") {
    throw new UiContractError("model is required");
  }
  return Object.freeze({
    landmarks: Object.freeze([
      Object.freeze({ element: "header", label: "AutoTrade status" }),
      Object.freeze({ element: "nav", label: "Primary navigation" }),
      Object.freeze({ element: "main", label: "Workspace" }),
      Object.freeze({ element: "aside", label: "Notifications" }),
    ]),
    navigation: Object.freeze([
      "Overview",
      "Accounts and host",
      "Opportunities and decisions",
      "Portfolio and orders",
      "Risk and authority",
      "Research and replay",
      "Learning and memory",
      "Models and costs",
      "History and diagnostics",
      "Settings",
    ]),
    status: Object.freeze({
      stale: Boolean(model.stale),
      text: model.stale
        ? "Data is stale. Last known values remain visible."
        : "Host data is current for the displayed event cursor.",
      livePoliteness: "polite",
    }),
    rules: Object.freeze({
      chartsRequireEquivalentTable: true,
      criticalValuesSelectableText: true,
      rowActionsKeyboardReachable: true,
      focusMustNotMoveOnPriceUpdate: true,
      acceptedCommandNeverRenderedAsCompleted: true,
      unknownNeverRenderedAsSuccess: true,
    }),
  });
}
