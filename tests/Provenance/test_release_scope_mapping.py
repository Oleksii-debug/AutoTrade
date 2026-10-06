from __future__ import annotations

import base64
from hashlib import sha256, sha512
import json
import unittest

from tools.release_scope_mapping import (
    ReleaseScopeMappingError,
    build_mapping,
)

SOURCE_SHA = "a" * 40
CONTENT_HASH = base64.b64encode(sha512(b"nupkg").digest()).decode("ascii")
CONTENT_HEX = sha512(b"nupkg").hexdigest()


def _sbom(*, extra=False, wrong_hash=False):
    packages = [{
        "SPDXID": "SPDXRef-WebView2",
        "name": "Microsoft.Web.WebView2",
        "versionInfo": "1.0.4258.31",
        "externalRefs": [{
            "referenceType": "purl",
            "referenceLocator":
                "pkg:nuget/Microsoft.Web.WebView2@1.0.4258.31",
        }],
        "checksums": [{
            "algorithm": "SHA512",
            "checksumValue": "0" * 128 if wrong_hash else CONTENT_HEX,
        }],
    }]
    if extra:
        packages.append({
            "SPDXID": "SPDXRef-Unknown",
            "name": "Unknown",
            "versionInfo": "1.0",
            "externalRefs": [{
                "referenceType": "purl",
                "referenceLocator": "pkg:nuget/Unknown@1.0",
            }],
            "checksums": [{
                "algorithm": "SHA512",
                "checksumValue": "1" * 128,
            }],
        })
    document = {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "packages": packages,
    }
    raw = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return document, raw


def _composition(sbom_raw):
    sbom_digest = "sha256:" + sha256(sbom_raw).hexdigest()
    dependency_digest = "sha256:" + "1" * 64
    return {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "source_sha": SOURCE_SHA,
        "dependency_lock_sha256": dependency_digest,
        "sbom_sha256": sbom_digest,
        "schema_compatibility": {"minimum": "1", "maximum": "1"},
        "runtime": {
            "architecture": "x64",
            "runtime_identifier": "win-x64",
            "minimum_windows_version": "10",
        },
        "components": [
            {
                "component_id": "lock",
                "kind": "dependency-lock",
                "path": "dependency-lock.json",
                "version": "1",
                "sha256": dependency_digest,
            },
            {
                "component_id": "sbom",
                "kind": "sbom",
                "path": "sbom.spdx.json",
                "version": "1",
                "sha256": sbom_digest,
            },
        ],
    }


LOCK = [{
    "name": "Microsoft.Web.WebView2",
    "version": "1.0.4258.31",
    "content_hash_sha512_base64": CONTENT_HASH,
}]
RIGHTS = [{
    "name": "Microsoft.Web.WebView2",
    "version": "1.0.4258.31",
    "content_hash_sha512_base64": CONTENT_HASH,
}]
PROVENANCE = [
    {
        "name": "Autosport first-party source",
        "repository": "Oleksii-debug/Autosport",
        "revision": "b" * 40,
        "release_scope_classification": "IMPORTED_FIRST_PARTY_SOURCE",
        "release_distribution_state": "BLOCKED",
    },
    {
        "name": "Nika Core first-party source",
        "repository": "Oleksii-debug/Nika-Core",
        "revision": "c" * 40,
        "release_scope_classification":
            "SEMANTIC_REFERENCE_NOT_DISTRIBUTED",
        "release_distribution_state": "BLOCKED",
    },
    {
        "name": "QuantConnect LEAN",
        "repository": "QuantConnect/Lean",
        "revision": "d" * 40,
        "release_scope_classification":
            "INSPECTED_CANDIDATE_NOT_DISTRIBUTED",
        "release_distribution_state": "BLOCKED",
    },
]


class ReleaseScopeMappingTests(unittest.TestCase):
    def _build(self, *, extra=False, wrong_hash=False, locked=None,
               rights=None, provenance=None):
        sbom, raw = _sbom(extra=extra, wrong_hash=wrong_hash)
        return build_mapping(
            composition=_composition(raw),
            sbom=sbom,
            sbom_raw=raw,
            locked_packages=LOCK if locked is None else locked,
            package_rights=RIGHTS if rights is None else rights,
            provenance_components=(
                PROVENANCE if provenance is None else provenance
            ),
        )

    def test_exact_webview2_mapping_keeps_imported_rights_blocker(self):
        result = self._build()
        self.assertEqual(
            result["distributed_packages"][0]["version"],
            "1.0.4258.31",
        )
        self.assertEqual(
            result["unresolved_distribution_rights"],
            ["Autosport first-party source"],
        )
        self.assertRegex(
            result["mapping_digest"], r"^sha256:[0-9a-f]{64}$"
        )

    def test_locked_package_without_rights_record_fails(self):
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "rights record"
        ):
            self._build(rights=[])

    def test_locked_package_missing_from_sbom_fails(self):
        other = {
            "name": "Other",
            "version": "1",
            "content_hash_sha512_base64": CONTENT_HASH,
        }
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "missing from SBOM"
        ):
            self._build(
                locked=LOCK + [other],
                rights=RIGHTS + [other],
            )

    def test_unknown_external_sbom_package_fails(self):
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "unmapped external"
        ):
            self._build(extra=True)

    def test_sbom_package_hash_mismatch_fails(self):
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "hash mismatch"
        ):
            self._build(wrong_hash=True)

    def test_unclassified_provenance_component_fails(self):
        value = dict(PROVENANCE[0])
        value.pop("release_scope_classification")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "release_scope_classification"
        ):
            self._build(provenance=[value])

    def test_mapping_is_order_stable(self):
        first = self._build()
        second = self._build(provenance=list(reversed(PROVENANCE)))
        self.assertEqual(
            first["mapping_digest"], second["mapping_digest"]
        )

    def test_changed_composition_changes_mapping_identity(self):
        sbom, raw = _sbom()
        composition = _composition(raw)
        first = build_mapping(
            composition=composition,
            sbom=sbom,
            sbom_raw=raw,
            locked_packages=LOCK,
            package_rights=RIGHTS,
            provenance_components=PROVENANCE,
        )
        composition["runtime"]["minimum_windows_version"] = "11"
        second = build_mapping(
            composition=composition,
            sbom=sbom,
            sbom_raw=raw,
            locked_packages=LOCK,
            package_rights=RIGHTS,
            provenance_components=PROVENANCE,
        )
        self.assertNotEqual(
            first["mapping_digest"], second["mapping_digest"]
        )


if __name__ == "__main__":
    unittest.main()
