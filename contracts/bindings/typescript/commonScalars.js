"use strict";

// TypeScript-consumable runtime binding for the canonical common scalar subset.
// Values remain strings; no Number/BigInt coercion is allowed.
const CONTRACT_VERSION = "2.0.0";

const patterns = Object.freeze({
  Decimal: /^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$/,
  Sequence: /^(0|[1-9][0-9]*)$/,
  Digest: /^sha256:[0-9a-f]{64}$/,
  CurrencyId: /^[A-Za-z0-9._:-]{1,32}$/,
  UnitId: /^[A-Za-z0-9._:/-]{1,64}$/,
});
const environments = new Set(["REPLAY", "SIMULATION", "PAPER", "LIVE"]);

function isValidCommonScalar(kind, value) {
  if (typeof value !== "string") return false;
  if (kind === "Environment") return environments.has(value);
  const pattern = patterns[kind];
  if (!pattern) throw new RangeError(`unsupported common scalar kind: ${kind}`);
  return pattern.test(value);
}

module.exports = { CONTRACT_VERSION, isValidCommonScalar };
