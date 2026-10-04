"use strict";

const fs = require("node:fs");
const path = require("node:path");
const {
  CONTRACT_VERSION,
  DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID,
  isValidDatasetManifestSemantics,
} = require("../../contracts/bindings/typescript/datasetManifest.js");

const corpusPath = path.resolve(
  process.cwd(),
  "contracts/fixtures/dataset-manifest.semantic.corpus.json"
);
const corpus = JSON.parse(fs.readFileSync(corpusPath, "utf8"));

if (corpus.contract_version !== CONTRACT_VERSION) {
  throw new Error(
    `corpus contract version ${corpus.contract_version} != binding ${CONTRACT_VERSION}`
  );
}
if (corpus.validator_id !== DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID) {
  throw new Error(
    `corpus validator ${corpus.validator_id} != binding ${DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID}`
  );
}

const names = new Set();
const failures = [];
for (const testCase of corpus.cases) {
  if (names.has(testCase.name)) {
    failures.push(`${testCase.name}: duplicate case name`);
    continue;
  }
  names.add(testCase.name);
  const actual = isValidDatasetManifestSemantics(testCase.value);
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
  console.log(
    `TypeScript DatasetManifest semantic corpus passed: ${names.size} cases.`
  );
}
