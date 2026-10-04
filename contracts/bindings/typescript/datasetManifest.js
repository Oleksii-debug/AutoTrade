"use strict";

const { CONTRACT_VERSION } = require("./commonScalars.js");

const DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID =
  "dataset-manifest-content-authority-v1";

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isValidDatasetManifestSemantics(value) {
  if (!isRecord(value)) return false;

  const contentHashes = value.content_hashes;
  if (!Array.isArray(contentHashes) || contentHashes.length === 0) return false;
  const declared = new Set();
  for (const digest of contentHashes) {
    if (typeof digest !== "string" || digest.length === 0 || declared.has(digest)) {
      return false;
    }
    declared.add(digest);
  }

  const evidence = value.source_evidence;
  if (!Array.isArray(evidence) || evidence.length === 0) return false;

  const referenced = new Set();
  for (const item of evidence) {
    if (!isRecord(item)) return false;
    const digest = item.sha256;
    if (typeof digest !== "string" || digest.length === 0) return false;
    referenced.add(digest);
  }
  for (const digest of declared) {
    if (!referenced.has(digest)) return false;
  }
  return true;
}

module.exports = {
  CONTRACT_VERSION,
  DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID,
  isValidDatasetManifestSemantics,
};
