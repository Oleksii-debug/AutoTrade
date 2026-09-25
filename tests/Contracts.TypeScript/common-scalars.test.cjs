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
const declarationPath = path.resolve(\n  process.cwd(),\n  "contracts/bindings/typescript/commonScalars.d.ts"\n);\nconst corpus = JSON.parse(fs.readFileSync(corpusPath, "utf8"));\nconst declaration = fs.readFileSync(declarationPath, "utf8");\nconst declarationVersion = declaration.match(\n  /export declare const CONTRACT_VERSION:\\s*"([^"]+)";/\n);\nif (!declarationVersion) {\n  throw new Error("TypeScript declaration does not expose literal CONTRACT_VERSION");\n}\nif (declarationVersion[1] !== CONTRACT_VERSION) {\n  throw new Error(\n    `TypeScript declaration version ${declarationVersion[1]} != runtime ${CONTRACT_VERSION}`\n  );\n}
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
