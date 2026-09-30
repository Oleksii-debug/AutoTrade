"use strict";

// AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.
// Run python tools/generate_common_scalar_bindings.py to regenerate.
const CONTRACT_VERSION = "5.0.0";

const patterns = Object.freeze({
  Decimal: /^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$(?![\s\S])/,
  Sequence: /^(0|[1-9][0-9]*)$(?![\s\S])/,
  Digest: /^sha256:[0-9a-f]{64}$(?![\s\S])/,
  CurrencyId: /^[A-Za-z0-9._:-]+$(?![\s\S])/,
  UnitId: /^[A-Za-z0-9._:\/-]+$(?![\s\S])/,
});
const lengths = Object.freeze({
  Decimal: Object.freeze([null, 259]),
  CurrencyId: Object.freeze([1, 32]),
  UnitId: Object.freeze([1, 64]),
});
const decimalEnvelopes = Object.freeze({
  Decimal: Object.freeze([256, 256, 256]),
});
const enums = Object.freeze({
  Environment: new Set(["REPLAY", "SIMULATION", "PAPER", "LIVE"]),
});

function withinDecimalEnvelope(value, limits) {
  const [maxSignificantDigits, maxScale, maxIntegerDigits] = limits;
  const unsigned = value.startsWith("-") ? value.slice(1) : value;
  const dotIndex = unsigned.indexOf(".");
  const integerPart = dotIndex === -1 ? unsigned : unsigned.slice(0, dotIndex);
  const fractionalPart = dotIndex === -1 ? "" : unsigned.slice(dotIndex + 1);
  const integerMagnitude = integerPart === "0" ? 0 : integerPart.length;
  const coefficient = integerPart + fractionalPart;
  const firstNonZero = coefficient.search(/[1-9]/);
  const significantDigits =
    firstNonZero === -1 ? 1 : coefficient.length - firstNonZero;
  return (
    significantDigits <= maxSignificantDigits &&
    fractionalPart.length <= maxScale &&
    integerMagnitude <= maxIntegerDigits
  );
}

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
  const match = pattern.exec(value);
  if (match === null || match.index !== 0 || match[0].length !== value.length) {
    return false;
  }
  const decimalLimits = decimalEnvelopes[kind];
  return !decimalLimits || withinDecimalEnvelope(value, decimalLimits);
}

module.exports = { CONTRACT_VERSION, isValidCommonScalar };
