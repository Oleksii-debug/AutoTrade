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
  await page.waitForFunction(count => document.querySelectorAll("#operations-body tr[data-operation-id]").length > count
    && document.querySelector("#operations-body").lastElementChild?.children[1]?.textContent === "SUCCEEDED", before);
  await tabTo(page, "refresh-state");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("#refresh-state").disabled
    && !document.querySelector("#submit-command").disabled
    && document.querySelector("#freshness").textContent.includes("host=CURRENT"));
  if (action === "RECOVER_SIMULATION" || action === "START_SIMULATION")
    await page.waitForFunction(() => document.querySelector("#portfolio-body").textContent.includes("895.696"));
}

(async () => {
  browser = await chromium.launch({
    ...(process.env.AUTOTRADE_BROWSER_PATH ? {executablePath: process.env.AUTOTRADE_BROWSER_PATH} : {}),
    args: ["--no-sandbox"],
  });
  const page = await browser.newPage();
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
  await stop();

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
