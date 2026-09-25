"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const binding = require(path.join(ROOT, "contracts", "bindings", "typescript", "commonScalars.js"));

function fail(message) {
  throw new Error(message);
}

function main() {
  const corpusPath = process.argv[2];
  const manifestPath = process.argv[3];
  if (!corpusPath || !manifestPath) {
    fail("usage: node common_scalar_conformance.js <corpus> <manifest>");
  }

  const corpus = JSON.parse(fs.readFileSync(path.resolve(corpusPath), "utf8"));
  const manifest = JSON.parse(fs.readFileSync(path.resolve(manifestPath), "utf8"));

  if (binding.CONTRACT_VERSION !== manifest.contract_version) {
    fail("TypeScript binding version does not match manifest");
  }
  if (corpus.contract_version !== manifest.contract_version) {
    fail("corpus contract version does not match manifest");
  }

  const seen = new Set();
  for (const testCase of corpus.cases) {
    if (seen.has(testCase.name)) {
      fail("duplicate corpus case: " + testCase.name);
    }
    seen.add(testCase.name);
    const actual = binding.isValidCommonScalar(testCase.type, testCase.value);
    if (actual !== testCase.expected) {
      fail(
        "TypeScript verdict mismatch for " +
          testCase.name +
          ": expected " +
          testCase.expected +
          ", got " +
          actual
      );
    }
  }

  process.stdout.write(
    "TypeScript common-scalar conformance passed (" +
      corpus.cases.length +
      " cases).\n"
  );
}

main();
