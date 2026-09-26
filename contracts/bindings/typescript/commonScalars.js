"use strict";

// AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.
// Run python tools/generate_common_scalar_bindings.py to regenerate.
const CONTRACT_VERSION = "4.0.0";

const patterns = Object.freeze({
  Decimal: /^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))(?![\s\S])/,
  Sequence: /^(0|[1-9][0-9]*)(?![\s\S])/,
  Digest: /^sha256:[0-9a-f]{64}(?![\s\S])/,
  CurrencyId: /^[A-Za-z0-9._:-]+(?![\s\S])/,
  UnitId: /^[A-Za-z0-9._:\/-]+(?![\s\S])/,
});
const lengths = Object.freeze({
  CurrencyId: Object.freeze([1, 32]),
  UnitId: Object.freeze([1, 64]),
});
const enums = Object.freeze({
  Environment: new Set(["REPLAY", "SIMULATION", "PAPER", "LIVE"]),
});

function isValidCommonScalar(kind, value) {
  if (typeof value !== "string") return false;
  const enumValues = enums[kind];
  if (enumValues) return enumValues.has(value);
  const pattern = patterns[kind];
  if (!pattern) throw new RangeError("unsupported common scalar kind: " + kind);
  const limit = lengths[kind];
  if (limit) {
    const [minimum, maximum] = limit;
    if (minimum !== null && value.length < minimum) return false;
    if (maximum !== null && value.length > maximum) return false;
  }
  return pattern.test(value);
}

module.exports = { CONTRACT_VERSION, isValidCommonScalar };
