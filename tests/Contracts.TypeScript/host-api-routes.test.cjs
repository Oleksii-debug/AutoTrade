"use strict";

global.window = {};
require("../../web/src/host-api-routes.js");

const api = global.window.AutoTradeHostApi;
if (!api || typeof api.route !== "function") {
  throw new Error("Generated host API binding was not installed.");
}

const expected = new Map([
  ["getState", "/api/v1/state"],
  ["submitCommand", "/api/v1/commands"],
  ["streamEvents", "/api/v1/events"],
  ["getHealth", "/api/v1/health"],
]);
for (const [operationId, route] of expected) {
  if (api.route(operationId) !== route) {
    throw new Error(
      operationId + ": expected " + route + ", got " + api.route(operationId)
    );
  }
}

const escaped = api.route(
  "getOperation",
  {operation_id: "a/b?c=d"}
);
if (escaped !== "/api/v1/operations/a%2Fb%3Fc%3Dd") {
  throw new Error("Path parameter was not encoded exactly.");
}

const strictEscaped = api.route(
  "getOperation",
  {operation_id: "!\'()*~"}
);
if (strictEscaped !== "/api/v1/operations/%21%27%28%29%2A~") {
  throw new Error(
    "Web path encoding must match RFC 3986 / Uri.EscapeDataString semantics."
  );
}

for (const invalid of ["", " ", " padded "]) {
  let failed = false;
  try {
    api.route("getOperation", {operation_id: invalid});
  } catch (error) {
    failed = error instanceof TypeError;
  }
  if (!failed) {
    throw new Error(
      "Invalid operation_id did not fail closed: " + JSON.stringify(invalid)
    );
  }
}

let unknownFailed = false;
try {
  api.route("notAnOperation");
} catch (error) {
  unknownFailed = error instanceof RangeError;
}
if (!unknownFailed) {
  throw new Error("Unknown operationId did not fail closed.");
}

let mutationFailed = false;
try {
  api.routes.getState = "/forged";
} catch (error) {
  mutationFailed = error instanceof TypeError;
}
if (!mutationFailed || api.routes.getState !== "/api/v1/state") {
  throw new Error("Generated route table must be immutable.");
}

console.log("Generated host API route binding passed.");
