"use strict";

const fs = require("node:fs");
const path = require("node:path");
const {
  CONTRACT_VERSION,
  isValidContractShape,
} = require("../../contracts/bindings/typescript/contractShapes.js");

const corpusPath = path.resolve(
  process.cwd(),
  "contracts/fixtures/contract-shapes.corpus.json"
);
const corpus = JSON.parse(fs.readFileSync(corpusPath, "utf8"));

const failures = [];
if (corpus.contract_version !== CONTRACT_VERSION) {
  failures.push(
    `corpus contract version ${corpus.contract_version} != binding ${CONTRACT_VERSION}`
  );
}
if (corpus.scope !== "closed-object-shape-subset") {
  failures.push(`unexpected corpus scope: ${corpus.scope}`);
}
if (corpus.cases.length !== corpus.case_count) {
  failures.push("corpus case_count does not match cases length");
}
const contracts = new Set(corpus.cases.map((testCase) => testCase.contract));
if (contracts.size !== corpus.definition_count) {
  failures.push("corpus definition_count does not match distinct contracts");
}
const dimensions = new Set(corpus.cases.map((testCase) => testCase.dimension));
for (const required of ["shape", "unknown-field", "required-field", "enum"]) {
  if (!dimensions.has(required)) failures.push(`missing dimension: ${required}`);
}

const names = new Set();
for (const testCase of corpus.cases) {
  if (names.has(testCase.name)) {
    failures.push(`${testCase.name}: duplicate case name`);
    continue;
  }
  names.add(testCase.name);
  let actual;
  try {
    actual = isValidContractShape(testCase.contract, testCase.value);
  } catch (error) {
    failures.push(`${testCase.name}: unexpected exception ${error}`);
    continue;
  }
  if (actual !== testCase.expected) {
    failures.push(
      `${testCase.name}: expected ${testCase.expected}, got ${actual}`
    );
  }
}

let unknownRejected = false;
try {
  isValidContractShape("missing.schema.json#/$defs/Missing", {});
} catch (error) {
  unknownRejected = error instanceof RangeError;
}
if (!unknownRejected) {
  failures.push("unknown contract identity was not rejected");
}

if (failures.length) {
  for (const failure of failures) console.error(failure);
  process.exitCode = 1;
} else {
  console.log(
    `TypeScript contract-shape corpus passed: ${names.size} cases across ${contracts.size} definitions.`
  );
}
