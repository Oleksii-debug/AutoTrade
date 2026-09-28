"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { CONTRACT_VERSION, isValidCommonScalar } = require(
  "../../contracts/bindings/typescript/commonScalars.js"
);

const corpusPath = path.resolve(
  process.cwd(),
  "contracts/fixtures/common-scalars.corpus.json"
);
const declarationPath = path.resolve(
  process.cwd(),
  "contracts/bindings/typescript/commonScalars.d.ts"
);
const corpus = JSON.parse(fs.readFileSync(corpusPath, "utf8"));
const declaration = fs.readFileSync(declarationPath, "utf8");
const declarationVersion = declaration.match(
  /export declare const CONTRACT_VERSION:\s*"([^"]+)";/
);
if (!declarationVersion) {
  throw new Error("TypeScript declaration does not expose literal CONTRACT_VERSION");
}
if (declarationVersion[1] !== CONTRACT_VERSION) {
  throw new Error(
    `TypeScript declaration version ${declarationVersion[1]} != runtime ${CONTRACT_VERSION}`
  );
}
if (corpus.contract_version !== CONTRACT_VERSION) {
  throw new Error(
    `corpus contract version ${corpus.contract_version} != binding ${CONTRACT_VERSION}`
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
