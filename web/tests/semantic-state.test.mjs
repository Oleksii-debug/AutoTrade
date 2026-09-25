import test from "node:test";
import assert from "node:assert/strict";

import {
  NotificationQueue,
  UiContractError,
  applyHostEvent,
  normalizeSnapshot,
  operationAnnouncement,
  semanticShellDescriptor,
} from "../src/semantic-state.mjs";

function snapshot(overrides = {}) {
  return {
    state_version: "0",
    event_cursor: "0",
    operations: {},
    ...overrides,
  };
}

function accepted(cursor = "1") {
  return {
    cursor,
    state_version: cursor,
    kind: "COMMAND_ACCEPTED",
    payload: {
      operation_id: "op-1",
      phase: "QUEUED",
      remaining_uncertainty: ["financial_outcome_not_completed"],
    },
  };
}

test("command acceptance stays distinct from financial completion", () => {
  const base = normalizeSnapshot(snapshot());
  const result = applyHostEvent(base, accepted());
  assert.equal(result.model.operations["op-1"].phase, "QUEUED");
  assert.equal(result.needsSnapshot, false);
  assert.match(result.announcement.text, /not completed/i);
});

test("UNKNOWN remains explicit uncertainty and never renders success", () => {
  const base = applyHostEvent(normalizeSnapshot(snapshot()), accepted()).model;
  const result = applyHostEvent(base, {
    cursor: "2",
    state_version: "2",
    kind: "OPERATION_UPDATED",
    payload: {
      operation_id: "op-1",
      phase: "UNKNOWN",
      remaining_uncertainty: ["provider_send_outcome_unknown"],
    },
  });
  assert.equal(result.model.operations["op-1"].phase, "UNKNOWN");
  assert.match(result.announcement.text, /unknown/i);
  assert.doesNotMatch(result.announcement.text, /completed according/i);
});

test("UNKNOWN without evidence fails closed", () => {
  const base = applyHostEvent(normalizeSnapshot(snapshot()), accepted()).model;
  assert.throws(
    () =>
      applyHostEvent(base, {
        cursor: "2",
        state_version: "2",
        kind: "OPERATION_UPDATED",
        payload: {
          operation_id: "op-1",
          phase: "UNKNOWN",
          remaining_uncertainty: [],
        },
      }),
    /UNKNOWN must retain explicit uncertainty/,
  );
});

test("event cursor gap marks all displayed values stale and requests snapshot", () => {
  const base = applyHostEvent(normalizeSnapshot(snapshot()), accepted()).model;
  const gap = applyHostEvent(base, {
    cursor: "4",
    state_version: "4",
    kind: "SOME_HOST_CHANGE",
    payload: {},
  });
  assert.equal(gap.needsSnapshot, true);
  assert.equal(gap.model.stale, true);
  assert.equal(gap.model.staleReason, "event_cursor_gap");
  assert.match(gap.announcement.text, /stale until a fresh snapshot/i);
});

test("event replay cannot move state version backwards", () => {
  const base = normalizeSnapshot(
    snapshot({ state_version: "9", event_cursor: "9" }),
  );
  assert.throws(
    () =>
      applyHostEvent(base, {
        cursor: "10",
        state_version: "8",
        kind: "SOME_HOST_CHANGE",
        payload: {},
      }),
    /cannot move backwards/,
  );
});

test("terminal operation cannot transition or retain uncertainty", () => {
  let model = applyHostEvent(normalizeSnapshot(snapshot()), accepted()).model;
  model = applyHostEvent(model, {
    cursor: "2",
    state_version: "2",
    kind: "OPERATION_UPDATED",
    payload: {
      operation_id: "op-1",
      phase: "SUCCEEDED",
      remaining_uncertainty: [],
    },
  }).model;
  assert.throws(
    () =>
      applyHostEvent(model, {
        cursor: "3",
        state_version: "3",
        kind: "OPERATION_UPDATED",
        payload: {
          operation_id: "op-1",
          phase: "RUNNING",
          remaining_uncertainty: [],
        },
      }),
    /terminal operation cannot transition/,
  );
  assert.throws(
    () => operationAnnouncement("op-x", "FAILED", ["still-unknown"]),
    /terminal operation cannot announce unresolved uncertainty/,
  );
});

test("notification queue coalesces repeated material events instead of tick flooding", () => {
  const queue = new NotificationQueue();
  const notice = operationAnnouncement("op-1", "WAITING_EXTERNAL");
  assert.equal(queue.enqueue(notice), true);
  assert.equal(queue.enqueue(notice), false);
  assert.equal(queue.drain().length, 1);
  assert.equal(queue.drain().length, 0);
});

test("semantic shell exposes all canonical regions and truthful accessibility rules", () => {
  const model = normalizeSnapshot(snapshot());
  const shell = semanticShellDescriptor(model);
  assert.equal(shell.landmarks.map((item) => item.element).join(","), "header,nav,main,aside");
  assert.equal(shell.navigation.length, 10);
  assert.equal(shell.rules.chartsRequireEquivalentTable, true);
  assert.equal(shell.rules.criticalValuesSelectableText, true);
  assert.equal(shell.rules.rowActionsKeyboardReachable, true);
  assert.equal(shell.rules.focusMustNotMoveOnPriceUpdate, true);
  assert.equal(shell.rules.acceptedCommandNeverRenderedAsCompleted, true);
  assert.equal(shell.rules.unknownNeverRenderedAsSuccess, true);
});

test("snapshot rejects unknown operation phase rather than normalizing it away", () => {
  assert.throws(
    () =>
      normalizeSnapshot(
        snapshot({
          operations: { "op-bad": "MAGIC_SUCCESS" },
        }),
      ),
    UiContractError,
  );
});

test("large integer cursors remain exact and do not pass through Number", () => {
  const huge = "900719925474099312345";
  const model = normalizeSnapshot(
    snapshot({ state_version: huge, event_cursor: huge }),
  );
  assert.equal(model.eventCursor.toString(), huge);
});
