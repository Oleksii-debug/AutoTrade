"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { isValidCommonScalar } = require(
  "../../contracts/bindings/typescript/commonScalars.js"
);

const corpusPath = path.resolve(
  process.cwd(),
  "contracts/fixtures/common-scalars.corpus.json"
);
const corpus = JSON.parse(fs.readFileSync(corpusPath, "utf8"));
if (corpus.contract_version !== "1.0.0") {
  throw new Error(`unexpected contract version: ${corpus.contract_version}`);
}

const names = new Set();
const failures = [];
for (const testCase of corpus.cases) {
  if (names.has(testCase.name)) {
    failures.push(`${testCase.name}: duplicate case name`);
    continue;
  }
  names.add(testCase.name);
  const actual = isValidCommonScalar(testCase.type, testCase.value);
  if (actual !== testCase.expected) {
    failures.push(
      `${testCase.name}: expected ${testCase.expected}, got ${actual}`
    );
  }
}
if (failures.length) {
  for (const failure of failures) console.error(failure);
  process.exitCode = 1;
} else {
  console.log(`TypeScript runtime common scalar corpus passed: ${names.size} cases.`);
}
