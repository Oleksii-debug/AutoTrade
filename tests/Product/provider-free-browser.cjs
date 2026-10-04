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
const env = {...process.env, PYTHONPATH: ROOT + path.delimiter + path.join(ROOT, "research")};
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "autotrade-browser-"));
const data = path.join(scratch, "product state with spaces #");
let host, browser, observedPage;
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

async function command(page, action, index) {
  stage = action;
  console.log("Keyboard command: " + action);
  await tabTo(page, "host-action");
  await page.keyboard.press("Home");
  for (let step = 0; step < index; step++) await page.keyboard.press("ArrowDown");
  assert.equal(await page.locator("#host-action").inputValue(), action);
  await page.waitForFunction(() => !document.querySelector("#submit-command").disabled);
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "submit-command", action);
  const before = await page.locator("#operations-body tr[data-operation-id]").count();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.activeElement?.id === "command-result");
  assert.equal(
    await page.evaluate(() => document.activeElement.id),
    "command-result",
    action + " command feedback focus");
  await page.waitForFunction(count => document.querySelectorAll("#operations-body tr[data-operation-id]").length > count
    && document.querySelector("#operations-body").lastElementChild?.children[1]?.textContent === "SUCCEEDED", before);
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
    await page.waitForFunction(() => document.querySelector("#portfolio-body").textContent.includes("895.696"));
}

async function exercisePortfolioTableTools(page) {
  stage = "portfolio keyboard tools";
  await tabTo(page, "portfolio-filter");
  await page.keyboard.type("895.696");
  await page.waitForFunction(() => {
    const status = document.querySelector("#portfolio-filter-status")?.textContent || "";
    const rows = [...document.querySelectorAll('#portfolio-body tr[data-filterable-row="true"]')];
    return status.includes("match the current filter") && rows.some(row => !row.hidden);
  });
  await page.waitForFunction(() =>
    (document.querySelector("#polite-status")?.textContent || "").includes(
      "rows match the current filter."));
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
  assert.match(await page.evaluate(() => navigator.clipboard.readText()), /895\.696/);
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "portfolio-filter");
  await page.keyboard.press("Control+A");
  await page.keyboard.press("Backspace");
  await page.waitForFunction(() =>
    (document.querySelector("#portfolio-filter-status")?.textContent || "").endsWith(" rows shown."));
}

async function exerciseSnapshotSelectionPreservation(page) {
  stage = "same-scope snapshot text selection";
  const selected = await page.evaluate(() => {
    const cell = [...document.querySelectorAll("#portfolio-body td")]
      .find(candidate => candidate.textContent.includes("895.696"));
    if (!cell || !cell.firstChild) return null;
    const value = cell.firstChild.data;
    const start = value.indexOf("895.696");
    if (start < 0) return null;
    const range = document.createRange();
    range.setStart(cell.firstChild, start);
    range.setEnd(cell.firstChild, start + "895.696".length);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return selection.toString();
  });
  assert.equal(selected, "895.696", "portfolio evidence is selectable before refresh");

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
          body.contains(range.startContainer) && body.contains(range.endContainer))
      };
    });
    assert.deepEqual(
      after,
      {text: "895.696", inside: true},
      "same-scope canonical snapshot preserves selected portfolio evidence");
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
  await page.keyboard.press("Tab");
  assert.equal(await page.locator(":focus").innerText(), "Skip to main content");
  await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(() => document.activeElement.id), "main");
  assert.equal(await page.locator("#polite-status").getAttribute("role"), "status");
  assert.equal(await page.locator("#urgent-status").getAttribute("role"), "alert");
  await exerciseSnapshotBusyFailClosed(page);
  await stop();
  await exerciseHostOutageFailClosed(page);

  const crash = spawnSync(python, ["-B", "-c", `
import os,sys
from pathlib import Path
import mvp.autotrade_mvp.simulation_session as s
from mvp.autotrade_mvp.simulation_commands import _protocol
from mvp.autotrade_mvp.persistence import JournalStore
root=Path(sys.argv[1]);p=_protocol(JournalStore(root/'journal.sqlite3'))
original=s.commit_order_fill_with_reservation_consumption
def die(*a,**kw):
    original(*a,**kw)
    os._exit(73)
s.commit_order_fill_with_reservation_consumption=die
s.run_autonomous_simulation(p['prices'],root,run_id=p['run_id'],now=p['start_time'],partial_fills=True)
`, path.join(data, "state")], {cwd: ROOT, env, encoding: "utf8", timeout: 30000});
  assert.equal(crash.status, 73, crash.stderr);
  await page.goto(await start(data));
  await page.waitForFunction(() => !document.querySelector("#submit-command").disabled);
  assert.match(await page.locator("#portfolio-body").innerText(), /PARTIALLY_FILLED/);
  await command(page, "RECOVER_SIMULATION", 3);
  assert.match(await page.locator("#portfolio-body").innerText(), /895\.696/);
  await exercisePortfolioTableTools(page);
  await exerciseSnapshotSelectionPreservation(page);
  assert.match(await page.locator("#strategy-body").innerText(), /deterministic-trend/);
  await command(page, "START_SIMULATION", 2);
  await command(page, "BACKUP_SIMULATION", 4);
  const backups = fs.readdirSync(path.join(data, "backups")).filter(name => /^[0-9a-f-]{36}$/.test(name));
  assert.equal(backups.length, 1);
  await command(page, "BLOCK_NEW_EXPOSURE", 0);
  await stop();
  const restored = path.join(scratch, "restored state");
  await page.goto(await start(restored, path.join(data, "backups", backups[0])));
  await page.waitForFunction(() => !document.querySelector("#submit-command").disabled);
  assert.match(await page.locator("#portfolio-body").innerText(), /895\.696/);
  assert.match(await page.locator("#risk-body").innerText(), /RECONCILIATION_REQUIRED/);
  await command(page, "RECOVER_SIMULATION", 3);
  await stop();
  assert.deepEqual(errors, []);
  passed = true;
  console.log(JSON.stringify({scenario: "provider-free-browser-crash-restart-backup-restore", passed: true,
    browser: browser.version(), keyboard: true, nvda_verified: false, real_orders: false}));
})().catch(async error => {
  console.error(error.message);
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
