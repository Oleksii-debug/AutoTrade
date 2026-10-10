"use strict";
// Real browser and real installed host; keyboard actions use no mouse.
// This evidence is not a Windows 11 / NVDA attestation.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const {spawn, spawnSync} = require("node:child_process");
const {chromium} = require(process.env.AUTOTRADE_PLAYWRIGHT_MODULE || "playwright");
const ROOT = process.env.AUTOTRADE_PRODUCT_ROOT || path.resolve(__dirname, "../..");
const python = process.env.AUTOTRADE_PYTHON || "python";
const env = {...process.env, AUTOTRADE_TEST_DIAGNOSTIC: "1", PYTHONPATH: ROOT + path.delimiter + path.join(ROOT, "research")};
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "autotrade-browser-"));
const data = path.join(scratch, "product state with spaces #");
let host, browser, observedPage, lastHostData;
let stage = "launch";
let passed = false;
const deadline = setTimeout(() => {
  console.error("Whole browser scenario did not finish: " + stage);
  if (host) host.kill("SIGKILL");
  process.exit(1);
}, 300000);
process.on("exit", () => {
  if (!passed) process.exitCode = 1;
});

async function start(directory, restore) {
  lastHostData = directory;
  const args = ["-B", "-m", "mvp.autotrade_mvp.product_runtime", "--data-dir", directory,
    "--port", "0", "--no-browser", "--desktop-child"];
  if (restore) args.push("--restore-backup", restore);
  host = spawn(python, args, {cwd: ROOT, env, stdio: ["pipe", "pipe", "pipe"]});
  let stdout = "", error = "";
  host.stdout.on("data", chunk => {stdout += chunk;});
  host.stderr.on("data", chunk => {error += chunk;});
  const deadline = Date.now() + 30000;
  while (!stdout.includes("\n")) {
    if (host.exitCode !== null || Date.now() > deadline) throw new Error("Host startup failed: " + error);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  // Pairing material is consumed without printing or preserving it in evidence.
  return stdout.split("\n")[0].slice("AutoTrade ZERO: ".length);
}

async function stop() {
  if (!host) return;
  const running = host;
  host = null;
  if (running.exitCode === null && running.signalCode === null) {
    const exited = new Promise(resolve => running.once("exit", resolve));
    running.stdin.write("STOP\n");
    const timer = setTimeout(() => running.kill("SIGKILL"), 30000);
    await exited;
    clearTimeout(timer);
  }
}

async function tabTo(page, id) {
  for (let count = 0; count < 160; count++) {
    if (await page.evaluate(() => document.activeElement.id) === id) return;
    await page.keyboard.press("Tab");
  }
  throw new Error("Keyboard could not reach " + id);
}

async function command(page, action) {
  stage = action;
  console.log("Keyboard command: " + action);
  // Resolve the canonical action by stable value, not historical option
  // order. The user interaction remains entirely keyboard-only.
  const index = await page.locator("#host-action option").evaluateAll(
    (options, target) => options.findIndex(option => option.value === target),
    action);
  assert.ok(index >= 0, "canonical Host action option is missing: " + action);
  await tabTo(page, "host-action");
  await page.keyboard.press("Home");
  for (let step = 0; step < index; step++) await page.keyboard.press("ArrowDown");
  assert.equal(await page.locator("#host-action").inputValue(), action);
  await page.waitForFunction(() => !document.querySelector("#submit-command").disabled);
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "submit-command", action);
  // Capture the actual same-origin authenticated Host response while issuing
  // the command by keyboard. A snapshot cursor gap correctly discards
  // incomplete event-derived UI rows; it must never be mistaken for a failed
  // or completed financial operation.
  const acceptedResponse = page.waitForResponse(response =>
    response.request().method() === "POST" &&
    new URL(response.url()).pathname === "/api/v1/commands");
  await page.keyboard.press("Enter");
  const response = await acceptedResponse;
  assert.equal(response.status(), 200, action + " canonical Host acceptance");
  const receipt = await response.json();
  assert.equal(receipt.status, "ACCEPTED", action + " admission only, not a fill");
  assert.match(receipt.operation_id, /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i);
  // Focus must still reach the real result and acceptance must be described
  // as provisional regardless of how long restored reconciliation takes.
  await page.waitForFunction(
    () => document.activeElement?.id === "command-result",
    null, {timeout: action === "RECOVER_SIMULATION" ? 120000 : 30000});
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "command-result",
    action + " command feedback focus");
  // Read the one canonical durable operation after the accepted command.
  // The UI intentionally drops event-derived operation rows when a snapshot
  // advances past unconsumed events. Never weaken that fail-closed UI behavior
  // or infer completion from ACCEPTED, a timeout, or portfolio figures.
  await page.waitForFunction(async operationId => {
    const route = window.AutoTradeHostApi.route("getOperation", {operation_id: operationId});
    const current = await fetch(route, {credentials: "same-origin", cache: "no-store",
      headers: {"Accept": "application/json"}});
    if (!current.ok) return false;
    const operation = await current.json();
    return operation.operation_id === operationId &&
      operation.phase === "SUCCEEDED" &&
      Array.isArray(operation.remaining_uncertainty) &&
      operation.remaining_uncertainty.length === 0;
  }, receipt.operation_id, {polling: 500,
    timeout: action === "RECOVER_SIMULATION" ? 120000 : 30000});
  const renderedOperation = page.locator('#operations-body tr[data-operation-id="' +
    receipt.operation_id + '"]');
  if (await renderedOperation.count() === 1) {
    assert.equal(await renderedOperation.locator("td").first().innerText(), "SUCCEEDED");
  } else {
    assert.match(await page.locator("#operations-body").innerText(),
      /Current host operations were cleared because Canonical snapshot advanced from event cursor/);
    assert.match(await page.locator("#command-result").innerText(),
      /was accepted for processing\. It is not yet a completed financial outcome/);
  }
  await page.keyboard.press("Shift+Tab");
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "refresh-state",
    action + " refresh reverse focus");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("#refresh-state").disabled
    && !document.querySelector("#submit-command").disabled
    && document.querySelector("#freshness").textContent.includes("host=CURRENT"));
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "refresh-state",
    action + " refresh completion focus");
  await page.keyboard.press("Shift+Tab");
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "submit-command",
    action + " submit reverse focus");
  await page.keyboard.press("Shift+Tab");
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "host-action",
    action + " action reverse focus");
  if (action === "RECOVER_SIMULATION" || action === "START_SIMULATION")
    await page.waitForFunction(() => document.querySelector("#portfolio-body").textContent.includes("791.392"));
}

async function exercisePortfolioTableTools(page) {
  stage = "portfolio keyboard tools";
  await tabTo(page, "portfolio-filter");
  await page.keyboard.type("791.392");
  await page.waitForFunction(() => {
    const status = document.querySelector("#portfolio-filter-status")?.textContent || "";
    const rows = [...document.querySelectorAll('#portfolio-body tr[data-filterable-row="true"]')];
    return status.includes("matching rows") && rows.some(row => !row.hidden);
  });
  await page.waitForFunction(() =>
    (document.querySelector("#polite-status")?.textContent || "").includes(
      "matching rows"));
  assert.equal(await page.evaluate(() => document.activeElement.id), "portfolio-filter");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "portfolio-copy");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() =>
    (document.querySelector("#portfolio-filter-status")?.textContent || "").includes(
      "visible portfolio rows copied."));
  await page.waitForFunction(() =>
    (document.querySelector("#polite-status")?.textContent || "").includes(
      "visible portfolio rows copied."));
  const copiedPortfolio = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(
    copiedPortfolio,
    /^Field\tHost evidence\r?\n/,
    "copied portfolio page is self-describing with column headings on Windows and POSIX");
  assert.match(copiedPortfolio, /791\.392/);
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "portfolio-filter");
  await page.keyboard.press("Control+A");
  await page.keyboard.press("Backspace");
  await page.waitForFunction(() => {
    const status = document.querySelector("#portfolio-filter-status")?.textContent || "";
    return status.includes("Page 1 of ") && status.includes("Sort: host order.");
  });
}

async function exercisePortfolioPagingAndSort(page) {
  stage = "portfolio paged reading and stable sort";
  await page.evaluate(() => {
    const body = document.querySelector("#portfolio-body");
    for (let index = 0; index < 30; index += 1) {
      const suffix = String(index).padStart(2, "0");
      const row = document.createElement("tr");
      row.dataset.filterableRow = "true";
      row.dataset.selectionKey = "paging-fixture:" + suffix;
      row.dataset.selectionExact = "true";
      const header = document.createElement("th");
      header.scope = "row";
      header.textContent = "paging-fixture-" + suffix;
      const cell = document.createElement("td");
      cell.textContent = "fixture-value-" + suffix;
      row.append(header, cell);
      body.appendChild(row);
    }
    const filter = document.querySelector("#portfolio-filter");
    filter.value = "paging-fixture-";
    filter.dispatchEvent(new Event("input", {bubbles: true}));
  });
  await page.waitForFunction(() =>
    (document.querySelector("#portfolio-filter-status")?.textContent || "").includes(
      "Rows 1-25 of 30 matching rows shown. Page 1 of 2. Sort: host order."));
  assert.equal(
    await page.locator('#portfolio-body tr[data-filterable-row="true"]:not([hidden])').count(),
    25,
    "first bounded page exposes exactly 25 matching rows");

  await page.locator("#portfolio-next").click();
  await page.waitForFunction(() =>
    (document.querySelector("#portfolio-filter-status")?.textContent || "").includes(
      "Rows 26-30 of 30 matching rows shown. Page 2 of 2. Sort: host order."));
  assert.equal(
    await page.locator('#portfolio-body tr[data-filterable-row="true"]:not([hidden])').count(),
    5,
    "second bounded page exposes the remaining five rows");

  await page.selectOption("#portfolio-sort", "text-desc");
  await page.waitForFunction(() =>
    (document.querySelector("#portfolio-filter-status")?.textContent || "").includes(
      "Page 1 of 2. Sort: rendered text descending."));
  const firstVisible = await page
    .locator('#portfolio-body tr[data-filterable-row="true"]:not([hidden])')
    .first()
    .innerText();
  assert.match(firstVisible, /paging-fixture-29/,
    "descending rendered-text sort is deterministic and restarts at page one");

  await page.evaluate(() => {
    const filter = document.querySelector("#portfolio-filter");
    filter.value = "";
    filter.dispatchEvent(new Event("input", {bubbles: true}));
    const sort = document.querySelector("#portfolio-sort");
    sort.value = "host";
    sort.dispatchEvent(new Event("change", {bubbles: true}));
    document.querySelector("#refresh-state").click();
  });
  await page.waitForFunction(() => document.querySelector("#refresh-state").disabled);
  await page.waitForFunction(() => !document.querySelector("#refresh-state").disabled);
  await page.waitForFunction(() =>
    document.querySelector("#portfolio-body").textContent.includes("791.392") &&
    !document.querySelector("#portfolio-body").textContent.includes("paging-fixture-"));
}

async function exerciseScopeSpeechIsolation(page) {
  stage = "display-scope speech isolation";
  await page.waitForTimeout(850);
  const current = await page.evaluate(async () => {
    const response = await fetch("/api/v1/state", {
      credentials: "same-origin",
      cache: "no-store",
      headers: {"Accept": "application/json"},
    });
    if (!response.ok) throw new Error("scope speech fixture snapshot failed");
    return response.json();
  });
  const shiftedHost = current.host_id + "-speech-probe";
  const shifted = {...current, host_id: shiftedHost};
  const routePattern = "**/api/v1/state";
  await page.route(routePattern, async route => {
    await route.fulfill({
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
      },
      body: JSON.stringify(shifted),
    });
  });
  try {
    await page.evaluate(() => {
      const filter = document.querySelector("#portfolio-filter");
      filter.value = "old-context-speech-probe-no-match";
      filter.dispatchEvent(new Event("input", {bubbles: true}));
      document.querySelector("#refresh-state").click();
    });
    await page.waitForFunction(expectedHost => {
      const host = document.querySelector("#active-host")?.textContent || "";
      const refresh = document.querySelector("#refresh-state");
      return host === expectedHost && refresh && !refresh.disabled;
    }, shiftedHost);
    await page.waitForTimeout(850);

    const polite = await page.locator("#polite-status").innerText();
    assert.doesNotMatch(
      polite,
      /0 of [0-9]+ rows match the current filter/,
      "queued old-context table speech must not cross a host display-scope reset");
    assert.match(
      polite,
      /reset for new account\/environment scope|Host display context changed/,
      "new display-scope feedback remains available after stale speech is discarded");
  } finally {
    await page.unroute(routePattern).catch(() => {});
    await page.evaluate(() => document.querySelector("#refresh-state").click());
    await page.waitForFunction(expectedHost => {
      const host = document.querySelector("#active-host")?.textContent || "";
      const refresh = document.querySelector("#refresh-state");
      return host === expectedHost && refresh && !refresh.disabled;
    }, current.host_id);
    await page.waitForTimeout(850);
  }
}

async function exerciseSnapshotSelectionPreservation(page) {
  stage = "same-scope snapshot text selection";
  const selected = await page.evaluate(() => {
    const cell = [...document.querySelectorAll("#portfolio-body td")]
      .find(candidate => candidate.textContent.includes("791.392"));
    if (!cell || !cell.firstChild) return null;
    const value = cell.firstChild.data;
    const start = value.indexOf("791.392");
    if (start < 0) return null;
    const range = document.createRange();
    range.setStart(cell.firstChild, start);
    range.setEnd(cell.firstChild, start + "791.392".length);
    const selection = window.getSelection();
    selection.removeAllRanges();
    if (typeof selection.setBaseAndExtent === "function") {
      selection.setBaseAndExtent(
        cell.firstChild, start + "791.392".length, cell.firstChild, start);
    } else {
      selection.addRange(range);
    }
    return {
      text: selection.toString(),
      backward: selection.anchorNode === cell.firstChild &&
        selection.focusNode === cell.firstChild &&
        selection.anchorOffset > selection.focusOffset
    };
  });
  assert.equal(selected.text, "791.392", "portfolio evidence is selectable before refresh");

  const routePattern = "**/api/v1/state";
  await page.route(routePattern, async route => {
    await new Promise(resolve => setTimeout(resolve, 100));
    await route.continue();
  });
  try {
    // Invoke the same user refresh path without moving focus/selection to the
    // button. This isolates the DOM-update invariant: a same-scope snapshot
    // must not erase ordinary selectable/copyable financial evidence.
    await page.evaluate(() => document.querySelector("#refresh-state").click());
    await page.waitForFunction(() => document.querySelector("#refresh-state").disabled);
    await page.waitForFunction(() => !document.querySelector("#refresh-state").disabled);
    const after = await page.evaluate(() => {
      const selection = window.getSelection();
      const range = selection && selection.rangeCount === 1 ? selection.getRangeAt(0) : null;
      const body = document.querySelector("#portfolio-body");
      return {
        text: selection ? selection.toString() : "",
        inside: Boolean(range && body &&
          body.contains(range.startContainer) && body.contains(range.endContainer)),
        backward: Boolean(selection && selection.anchorNode && selection.focusNode &&
          selection.anchorOffset > selection.focusOffset)
      };
    });
    assert.equal(after.text, "791.392",
      "same-scope canonical snapshot preserves selected portfolio evidence");
    assert.equal(after.inside, true,
      "restored selection remains inside the portfolio evidence table");
    if (selected.backward) {
      assert.equal(after.backward, true,
        "backward selection direction survives snapshot replacement");
    }
  } finally {
    await page.unroute(routePattern).catch(() => {});
  }
}

async function exerciseSnapshotBusyFailClosed(page) {
  stage = "snapshot busy keyboard refresh";
  let busy = true;
  const routePattern = "**/api/v1/state";
  const urgentBefore = await page.locator("#urgent-status").innerText();
  await page.route(routePattern, async route => {
    if (!busy) {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 503,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Retry-After": "1",
      },
      body: JSON.stringify({error: "SNAPSHOT_BUSY", retryable: true}),
    });
  });
  try {
    await tabTo(page, "refresh-state");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => {
      const refresh = document.querySelector("#refresh-state");
      const submit = document.querySelector("#submit-command");
      const freshness = document.querySelector("#freshness")?.textContent || "";
      const polite = document.querySelector("#polite-status")?.textContent || "";
      return refresh && !refresh.disabled && submit && submit.disabled
        && /temporarily busy/i.test(freshness)
        && /waiting for one coherent snapshot/i.test(polite);
    });
    assert.equal(
      await page.evaluate(() => document.activeElement.id),
      "refresh-state",
      "snapshot-busy keyboard refresh focus");
    assert.equal(await page.locator("#urgent-status").innerText(), urgentBefore);
    busy = false;
    await page.unroute(routePattern);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => {
      const refresh = document.querySelector("#refresh-state");
      const submit = document.querySelector("#submit-command");
      const freshness = document.querySelector("#freshness")?.textContent || "";
      return refresh && !refresh.disabled && submit && !submit.disabled
        && freshness.includes("host=CURRENT");
    });
    assert.equal(
      await page.evaluate(() => document.activeElement.id),
      "refresh-state",
      "snapshot-busy recovery focus");
  } finally {
    busy = false;
    await page.unroute(routePattern).catch(() => {});
  }
}

async function exerciseHostOutageFailClosed(page) {
  stage = "host outage keyboard refresh";
  await tabTo(page, "refresh-state");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => {
    const refresh = document.querySelector("#refresh-state");
    const submit = document.querySelector("#submit-command");
    const freshness = document.querySelector("#freshness")?.textContent || "";
    const urgent = document.querySelector("#urgent-status")?.textContent || "";
    return refresh && !refresh.disabled && submit && submit.disabled
      && /unavailable|stale/i.test(freshness)
      && /failed|unavailable|stale/i.test(urgent);
  });
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "refresh-state",
    "failed keyboard refresh focus");
  assert.match(await page.locator("#freshness").innerText(), /unavailable|stale/i);
  assert.match(await page.locator("#urgent-status").innerText(), /failed|unavailable|stale/i);
}

async function exerciseCanonicalPageNavigation(page) {
  stage = "canonical semantic web navigation and keyboard history";
  const ids = [
    "overview", "accounts", "opportunities", "portfolio", "risk",
    "research", "learning", "models", "history", "settings"
  ];
  for (const id of ids) {
    const anchor = page.locator('nav[aria-label="Primary"] a[href="#' + id + '"]');
    assert.equal(await anchor.count(), 1, "one canonical link for " + id);
    assert.equal(await page.locator("#" + id + " h2").count(), 1);
  }
  const first = page.locator('nav a[href="#portfolio"]');
  await first.focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => location.hash === "#portfolio" &&
    document.activeElement?.id === "portfolio-heading" &&
    document.querySelector('nav a[href="#portfolio"]')?.getAttribute("aria-current") === "location");
  assert.match(await page.locator("#page-navigation-status").innerText(), /Portfolio and orders/);
  await page.goBack();
  await page.waitForFunction(() => location.hash === "#main" &&
    document.querySelectorAll('nav [aria-current="location"]').length === 0);
  await page.goForward();
  await page.waitForFunction(() => location.hash === "#portfolio" &&
    document.activeElement?.id === "portfolio-heading");
  const before = await page.locator("#state-version").innerText();
  await page.evaluate(() => {
    location.hash = "#unknown-section";
  });
  await page.waitForFunction(() =>
    document.querySelector("#page-navigation-status")?.textContent.includes("Unknown section."));
  assert.equal(await page.locator('#polite-status').getAttribute("role"), "status");
  assert.equal(await page.locator("#state-version").innerText(), before,
    "unrecognized page routes do not rewrite host state");
  await page.locator('nav a[href="#risk"]').focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => location.hash === "#risk" &&
    document.activeElement?.id === "risk-heading");
  assert.equal(await page.locator("nav [aria-current]").count(), 1);
  assert.match(await page.locator("#provider-availability").innerText(), /UNAVAILABLE/);
  assert.match(await page.locator("#provider-availability").innerText(), /ZERO.SIMULATION/);
}

(async () => {
  browser = await chromium.launch({
    ...(process.env.AUTOTRADE_BROWSER_PATH ? {executablePath: process.env.AUTOTRADE_BROWSER_PATH} : {}),
    args: ["--no-sandbox"],
  });
  const context = await browser.newContext({
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  observedPage = page;
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(await start(data));
  await page.waitForFunction(() => !document.querySelector("#submit-command").disabled);
  assert.equal(new URL(page.url()).hash, "");
  assert.match(await page.locator("#jobs-body").innerText(), /DIAGNOSTIC_ONLY/);
  // Chromium may have pre-focused the skip link during automatic pairing.
  // Probe strictly by keyboard, and reverse one Tab when already past it.
  await page.keyboard.press("Tab");
  if (await page.locator(":focus").innerText() !== "Skip to main content") {
    await page.keyboard.press("Shift+Tab");
  }
  assert.equal(await page.locator(":focus").innerText(), "Skip to main content");
  await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(() => document.activeElement.id), "main");
  assert.equal(await page.locator("#polite-status").getAttribute("role"), "status");
  assert.equal(await page.locator("#urgent-status").getAttribute("role"), "alert");
  await exerciseCanonicalPageNavigation(page);
  await exerciseSnapshotBusyFailClosed(page);
  await stop();
  await exerciseHostOutageFailClosed(page);

  // A prior Host has already advanced its financial and control journal.
  // Crash-injection is only legitimate in a fresh simulator-owned scope:
  // reproduce exactly the canonical Product bootstrap protocol, and crash
  // after the first *real* atomic fill. A Host-signature is never bypassed.
  stage = "isolated financial crash preparation";
  const crashData = path.join(scratch, "isolated crash recovery state");
  // Bootstrap with the exact Product Host's canonical one-observation
  // simulator contract, but without starting a second Host process. Host
  // startup has its own durable research/Host journal writes: those are not
  // part of the simulator's signed checkpoint cut and must not be injected
  // between this independent crash process and its first financial admission.
  // This creates no synthetic fills, bypasses no signer, and leaves the real
  // packaged Host/recovery/browser acceptance below unchanged.
  const bootstrap = spawnSync(python, ["-B", "-c", `
from mvp.autotrade_mvp.product_runtime import PRICES, START_TIME
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation
from pathlib import Path
import sys
result = run_autonomous_simulation(
    PRICES, Path(sys.argv[1]).resolve(), run_id='provider-free-product',
    now=START_TIME, stop_after_episodes=1,
    execution_profile='TWO_EQUAL_PARTIALS', target_quantity='2')
print(result['status'], result['completed_episodes'])
`, path.join(crashData, "state")], {cwd: ROOT, env, encoding: "utf8", timeout: 30000});
  assert.equal(bootstrap.status, 0, bootstrap.stderr);
  assert.equal(bootstrap.stdout.trim(), "PAUSED 1");

  const crash = spawnSync(python, ["-B", "-c", `
import os,sys
from pathlib import Path
import mvp.autotrade_mvp.simulation_session as s
from mvp.autotrade_mvp.simulation_commands import _protocol
from mvp.autotrade_mvp.persistence import JournalStore
root=Path(sys.argv[1]).resolve(); p=_protocol(JournalStore(root/"journal.sqlite3"))
original=s.commit_order_fill_with_reservation_consumption
def die(*a,**kw):
    original(*a,**kw)
    os._exit(73)
s.commit_order_fill_with_reservation_consumption=die
s.run_autonomous_simulation(p['prices'],root,run_id=p['run_id'],now=p['start_time'],execution_profile=p['execution_profile'],target_quantity=p['target_quantity'])
`, path.join(crashData, "state")], {cwd: ROOT, env, encoding: "utf8", timeout: 30000});
  assert.equal(crash.status, 73, crash.stderr);
  await page.goto(await start(crashData));
  await page.waitForFunction(() => !document.querySelector("#submit-command").disabled);
  assert.match(await page.locator("#portfolio-body").innerText(), /PARTIALLY_FILLED/);
  await command(page, "RECOVER_SIMULATION");
  assert.match(await page.locator("#portfolio-body").innerText(), /791\.392/);
  await exercisePortfolioTableTools(page);
  await exercisePortfolioPagingAndSort(page);
  await exerciseSnapshotSelectionPreservation(page);
  await exerciseScopeSpeechIsolation(page);
  assert.match(await page.locator("#strategy-body").innerText(), /deterministic-trend/);
  await command(page, "START_SIMULATION");
  await command(page, "BACKUP_SIMULATION");
  const backups = fs.readdirSync(path.join(crashData, "backups")).filter(name => /^[0-9a-f-]{36}$/.test(name));
  assert.equal(backups.length, 1);
  await command(page, "BLOCK_NEW_EXPOSURE");
  await stop();
  const restored = path.join(scratch, "restored state");
  await page.goto(await start(restored, path.join(crashData, "backups", backups[0])));
  await page.waitForFunction(() => !document.querySelector("#submit-command").disabled);
  assert.match(await page.locator("#portfolio-body").innerText(), /791\.392/);
  assert.match(await page.locator("#risk-body").innerText(), /RECONCILIATION_REQUIRED/);
  await command(page, "RECOVER_SIMULATION");
  await stop();
  assert.deepEqual(errors, []);
  passed = true;
  console.log(JSON.stringify({scenario: "provider-free-browser-crash-restart-backup-restore", passed: true,
    browser: browser.version(), keyboard: true, nvda_verified: false, real_orders: false}));
})().catch(async error => {
  console.error(error.message);
  // Worker diagnostics are deliberately limited to fixed stage/error codes.
  // Never publish traceback, stderr, filesystem names, tokens or key bytes.
  if (lastHostData) {
    const stageFile = path.join(lastHostData, "worker-stage-diagnostic.txt");
    if (fs.existsSync(stageFile)) {
      const code = fs.readFileSync(stageFile, "utf8").trim();
      if (/^[A-Za-z0-9_.:-]{1,256}$/.test(code))
        console.error("Safe worker stage: " + code);
    }
  }
  if (observedPage) console.error(JSON.stringify({stage,
    result: await observedPage.locator("#command-result").innerText(),
    freshness: await observedPage.locator("#freshness").innerText(),
    operations: await observedPage.locator("#operations-body").innerText()}));
  process.exitCode = 1;
}).finally(async () => {
  await stop();
  if (browser) await browser.close();
  clearTimeout(deadline);
  fs.rmSync(scratch, {recursive: true, force: true});
});
