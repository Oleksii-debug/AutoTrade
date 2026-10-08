"use strict";
// Plan-5 source-level browser qualification on a deliberately unavailable Host.
// This does not assert financial authority, paper/live capability, or physical NVDA.
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const {chromium} = require("playwright");

const WEB = path.resolve(__dirname, "../../web/src");
const files = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ["/index.html", ["index.html", "text/html; charset=utf-8"]],
  ["/app.js", ["app.js", "text/javascript; charset=utf-8"]],
  ["/host-api-routes.js", ["host-api-routes.js", "text/javascript; charset=utf-8"]],
  ["/styles.css", ["styles.css", "text/css; charset=utf-8"]]
]);

async function main() {
  const server = http.createServer(async (request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname.startsWith("/api/")) {
      response.writeHead(503, {"Content-Type": "application/json", "Cache-Control": "no-store"});
      response.end('{"error":"HOST_UNAVAILABLE"}');
      return;
    }
    const file = files.get(pathname);
    if (!file) { response.writeHead(404); response.end("Not found"); return; }
    try {
      const content = await fs.readFile(path.join(WEB, file[0]));
      response.writeHead(200, {"Content-Type": file[1], "Cache-Control": "no-store"});
      response.end(content);
    } catch (error) { response.writeHead(500); response.end("Static fixture failure"); }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, args: ["--no-sandbox"]});
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    const origin = "http://127.0.0.1:" + server.address().port;
    await page.goto(origin + "/");
    const routes = ["overview", "accounts", "opportunities", "portfolio", "risk",
      "research", "learning", "models", "history", "settings"];
    for (const id of routes) {
      assert.equal(await page.locator('nav[aria-label="Primary"] a[href="#' + id + '"]').count(), 1);
      assert.equal(await page.locator('section#' + id + ' > h2#' + id + '-heading').count(), 1);
    }
    await page.waitForFunction(() => document.querySelector('nav a[href="#overview"]')?.getAttribute("aria-current") === "location");
    assert.equal(await page.locator("#submit-command").isDisabled(), true, "unavailable host must not enable financial commands");
    await page.keyboard.press("Tab");
    assert.match(await page.locator(":focus").innerText(), /Skip to main content/);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => location.hash === "#main" && document.activeElement.id === "main");
    await page.locator('nav a[href="#risk"]').focus();
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => location.hash === "#risk" &&
      document.activeElement?.id === "risk-heading" &&
      document.querySelector('nav a[href="#risk"]').getAttribute("aria-current") === "location");
    assert.match(await page.locator("#page-navigation-status").innerText(), /Risk and authority/);
    await page.goBack();
    await page.waitForFunction(() => location.hash === "#main" && !document.querySelector("nav [aria-current]"));
    await page.goForward();
    await page.waitForFunction(() => location.hash === "#risk" && document.activeElement.id === "risk-heading");
    await page.goto(origin + "/index.html#history");
    await page.waitForFunction(() => document.activeElement?.id === "history-heading" &&
      document.querySelector('nav a[href="#history"]').getAttribute("aria-current") === "location");
    await page.evaluate(() => { location.hash = "#unknown-section"; });
    await page.waitForFunction(() => document.querySelector("#page-navigation-status")?.textContent.includes("Unknown section."));
    assert.equal(await page.locator("nav [aria-current]").count(), 0);
    assert.equal(await page.locator("#submit-command").isDisabled(), true);
    assert.match(await page.locator("#provider-availability").innerText(), /UNAVAILABLE/);
    assert.match(await page.locator("#provider-availability").innerText(), /ZERO\/SIMULATION/);
    assert.deepEqual(pageErrors, [], "no runtime JavaScript exceptions during browser navigation");
    console.log("PLAN5 SECTION1 PASS: 10 semantic routes, keyboard/heading focus, browser history, deep link, unknown route, unavailable host");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => {console.error(error);process.exitCode = 1;});
