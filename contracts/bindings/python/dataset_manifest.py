"""Canonical cross-field semantics for DatasetManifest contract v6."""

from collections.abc import Mapping
from typing import Any

CONTRACT_VERSION = "6.0.0"
DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID = "dataset-manifest-content-authority-v1"


def is_valid_dataset_manifest_semantics(value: Any) -> bool:
    """Return whether every declared content digest has exact EvidenceRef authority.

    This validator supplements JSON Schema structural validation. Every digest
    in content_hashes must be named by at least one source EvidenceRef. Extra
    evidence whose digest is not a content hash remains provenance-only.
    """

    if not isinstance(value, Mapping):
        return False

    content_hashes = value.get("content_hashes")
    if (
        type(content_hashes) is not list
        or not content_hashes
        or any(type(item) is not str or not item for item in content_hashes)
        or len(content_hashes) != len(set(content_hashes))
    ):
        return False
    declared = set(content_hashes)

    source_evidence = value.get("source_evidence")
    if type(source_evidence) is not list or not source_evidence:
        return False

    referenced: set[str] = set()
    for evidence in source_evidence:
        if not isinstance(evidence, Mapping):
            return False
        digest = evidence.get("sha256")
        if type(digest) is not str or not digest:
            return False
        referenced.add(digest)
    return declared.issubset(referenced)
