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
        "SPDXID": "SPDXRef-Package-AutoTrade",
        "name": "AutoTrade",
        "versionInfo": SOURCE_SHA,
        "licenseConcluded": "NOASSERTION",
        "primaryPackagePurpose": "APPLICATION",
    }, {
        "SPDXID": "SPDXRef-WebView2",
        "name": "Microsoft.Web.WebView2",
        "versionInfo": "1.0.4258.31",
        "licenseConcluded": "BSD-3-Clause",
        "externalRefs": [{
            "referenceCategory": "PACKAGE_MANAGER",
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
            "licenseConcluded": "NOASSERTION",
            "externalRefs": [{
                "referenceCategory": "PACKAGE_MANAGER",
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
        "dataLicense": "CC0-1.0",
        "documentNamespace": (
            "https://autotrade.invalid/spdx/" + SOURCE_SHA
        ),
        "packages": packages,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-Package-AutoTrade",
            },
            {
                "spdxElementId": "SPDXRef-Package-AutoTrade",
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": "SPDXRef-WebView2",
            },
        ],
    }
    if extra:
        document["relationships"].append({
            "spdxElementId": "SPDXRef-Package-AutoTrade",
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": "SPDXRef-Unknown",
        })
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
    "license_id": "BSD-3-Clause",
}]
REUSE = [{
    "schema_version": "1.0.0",
    "source": {
        "repository": "Oleksii-debug/Autosport",
        "revision": "b" * 40,
        "rights_basis": "OWNER_AUTHORIZED_MIGRATION_FOR_PROJECT_DEVELOPMENT",
        "release_distribution_rights": "UNRESOLVED",
    },
    "migrations": [{
        "source_path": "src/autosport/json_integrity.py",
        "destination_path": "research/autotrade_research/io/strict_json.py",
        "symbols": ["strict_json_loads"],
        "runtime_dependency_on_autosport": False,
    }],
}]

EXTERNAL_RIGHTS = [{
    "purl": "pkg:generic/cpython-embed@3.12.10",
    "name": "CPython",
    "version": "3.12.10",
    "artifact_sha256": "sha256:" + "2" * 64,
    "license_concluded": "PSF-2.0",
    "release_distribution_state": "BLOCKED",
    "upstream_sbom_url": (
        "https://www.python.org/ftp/python/3.12.10/"
        "python-3.12.10-embed-amd64.zip.spdx.json"
    ),
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
               rights=None, provenance=None, reuse=None, sbom_override=None,
               external_rights=None):
        sbom, raw = _sbom(extra=extra, wrong_hash=wrong_hash)
        if sbom_override is not None:
            sbom, raw = sbom_override
        return build_mapping(
            composition=_composition(raw),
            sbom=sbom,
            sbom_raw=raw,
            locked_packages=LOCK if locked is None else locked,
            package_rights=RIGHTS if rights is None else rights,
            provenance_components=(
                PROVENANCE if provenance is None else provenance
            ),
            reuse_documents=REUSE if reuse is None else reuse,
            external_runtime_rights=(
                [] if external_rights is None else external_rights
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
            ReleaseScopeMappingError, "do not exactly match SBOM runtime scope"
        ):
            self._build(extra=True)




    def test_sbom_document_namespace_must_bind_composition_source(self):
        document, _ = _sbom()
        document["documentNamespace"] = (
            "https://autotrade.invalid/spdx/" + "f" * 40
        )
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "namespace differs from composition source",
        ):
            self._build(sbom_override=(document, raw))

    def test_sbom_purl_requires_package_manager_category(self):
        document, _ = _sbom()
        webview = next(
            item for item in document["packages"]
            if item["name"] == "Microsoft.Web.WebView2"
        )
        webview["externalRefs"][0]["referenceCategory"] = "OTHER"
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "reference category must be PACKAGE_MANAGER",
        ):
            self._build(sbom_override=(document, raw))

    def test_sbom_application_source_must_match_composition(self):
        document, _ = _sbom()
        application = next(
            item for item in document["packages"]
            if item["name"] == "AutoTrade"
        )
        application["versionInfo"] = "f" * 40
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "application identity differs from composition",
        ):
            self._build(sbom_override=(document, raw))

    def test_sbom_requires_autotrade_application_package(self):
        document, _ = _sbom()
        document["packages"] = [
            item for item in document["packages"]
            if item["name"] != "AutoTrade"
        ]
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "exactly one AutoTrade application",
        ):
            self._build(sbom_override=(document, raw))

    def test_sbom_requires_document_describes_application(self):
        document, _ = _sbom()
        document["relationships"] = []
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "describe the exact AutoTrade application",
        ):
            self._build(sbom_override=(document, raw))

    def test_sbom_duplicate_spdx_identity_fails(self):
        document, _ = _sbom()
        webview = next(
            item for item in document["packages"]
            if item["name"] == "Microsoft.Web.WebView2"
        )
        webview["SPDXID"] = "SPDXRef-Package-AutoTrade"
        dependency = next(
            relation for relation in document["relationships"]
            if relation.get("relationshipType") == "DEPENDS_ON"
        )
        dependency["relatedSpdxElement"] = "SPDXRef-Package-AutoTrade"
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "SPDXID is duplicated"
        ):
            self._build(sbom_override=(document, raw))


    def test_sbom_external_package_must_be_reachable_from_application(self):
        document, _ = _sbom()
        document["relationships"] = [
            relation
            for relation in document["relationships"]
            if relation.get("relationshipType") != "DEPENDS_ON"
        ]
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "not reachable from AutoTrade dependency graph",
        ):
            self._build(sbom_override=(document, raw))

    def test_sbom_dependency_cannot_reference_unknown_package(self):
        document, _ = _sbom()
        document["relationships"].append({
            "spdxElementId": "SPDXRef-WebView2",
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": "SPDXRef-Missing",
        })
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "references unknown package",
        ):
            self._build(sbom_override=(document, raw))

    def test_locked_package_license_mismatch_fails(self):
        document, raw = _sbom()
        webview = next(
            item for item in document["packages"]
            if item["name"] == "Microsoft.Web.WebView2"
        )
        webview["licenseConcluded"] = "MIT"
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "SBOM package license mismatch"
        ):
            self._build(sbom_override=(document, raw))

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

    def test_external_sbom_package_without_purl_fails(self):
        document, raw = _sbom()
        package = dict(next(
            item for item in document["packages"]
            if item["name"] == "Microsoft.Web.WebView2"
        ))
        package.pop("externalRefs")
        document = dict(document)
        document["packages"] = [
            next(
                item for item in document["packages"]
                if item["name"] == "AutoTrade"
            ),
            package,
        ]
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "lacks purl identity"
        ):
            self._build(sbom_override=(document, raw))

    def test_imported_source_requires_exact_reuse_manifest(self):
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "requires one exact reuse manifest"
        ):
            self._build(reuse=[])


    def test_orphan_reuse_manifest_fails_exact_scope_closure(self):
        orphan = json.loads(json.dumps(REUSE[0]))
        orphan["source"]["repository"] = "Oleksii-debug/Other"
        orphan["source"]["revision"] = "e" * 40
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "source outside imported release scope",
        ):
            self._build(reuse=REUSE + [orphan])

    def test_duplicate_reuse_source_identity_fails(self):
        duplicate = json.loads(json.dumps(REUSE[0]))
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "source identity is duplicated",
        ):
            self._build(reuse=REUSE + [duplicate])

    def test_reuse_migration_requires_canonical_source_path(self):
        broken = json.loads(json.dumps(REUSE[0]))
        broken["migrations"][0].pop("source_path")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "source_path"
        ):
            self._build(reuse=[broken])

    def test_reuse_migration_requires_nonempty_unique_symbols(self):
        broken = json.loads(json.dumps(REUSE[0]))
        broken["migrations"][0]["symbols"] = ["strict_json_loads"] * 2
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "symbols are duplicated"
        ):
            self._build(reuse=[broken])

    def test_imported_source_mapping_binds_destination_path(self):
        result = self._build()
        autosport = next(
            item for item in result["provenance_scope"]
            if item["name"] == "Autosport first-party source"
        )
        self.assertEqual(
            autosport["reuse_mapping"]["destination_paths"],
            ["research/autotrade_research/io/strict_json.py"],
        )
        self.assertEqual(
            autosport["reuse_mapping"]["release_distribution_rights"],
            "UNRESOLVED",
        )

    def test_external_runtime_is_mapped_and_kept_blocked(self):
        document, raw = _sbom()
        python_package = {
            "SPDXID": "SPDXRef-Python",
            "name": "CPython",
            "versionInfo": "3.12.10",
            "licenseConcluded": "PSF-2.0",
            "externalRefs": [{
                "referenceCategory": "PACKAGE_MANAGER",
                "referenceType": "purl",
                "referenceLocator":
                    "pkg:generic/cpython-embed@3.12.10",
            }],
            "checksums": [{
                "algorithm": "SHA256",
                "checksumValue": "2" * 64,
            }],
        }
        document["packages"].append(python_package)
        document["relationships"].append({
            "spdxElementId": "SPDXRef-Package-AutoTrade",
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": "SPDXRef-Python",
        })
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        result = self._build(
            sbom_override=(document, raw),
            external_rights=EXTERNAL_RIGHTS,
        )
        self.assertEqual(
            result["external_runtime_components"][0]["name"],
            "CPython",
        )
        self.assertIn(
            "CPython",
            result["unresolved_distribution_rights"],
        )


    def test_external_runtime_license_mismatch_fails(self):
        document, raw = _sbom()
        document["packages"].append({
            "SPDXID": "SPDXRef-Python",
            "name": "CPython",
            "versionInfo": "3.12.10",
            "licenseConcluded": "MIT",
            "externalRefs": [{
                "referenceCategory": "PACKAGE_MANAGER",
                "referenceType": "purl",
                "referenceLocator":
                    "pkg:generic/cpython-embed@3.12.10",
            }],
            "checksums": [{
                "algorithm": "SHA256",
                "checksumValue": "2" * 64,
            }],
        })
        document["relationships"].append({
            "spdxElementId": "SPDXRef-Package-AutoTrade",
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": "SPDXRef-Python",
        })
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "external runtime license mismatch"
        ):
            self._build(
                sbom_override=(document, raw),
                external_rights=EXTERNAL_RIGHTS,
            )

    def test_external_runtime_without_rights_record_fails(self):
        document, raw = _sbom()
        document["packages"].append({
            "SPDXID": "SPDXRef-Python",
            "name": "CPython",
            "versionInfo": "3.12.10",
            "licenseConcluded": "PSF-2.0",
            "externalRefs": [{
                "referenceCategory": "PACKAGE_MANAGER",
                "referenceType": "purl",
                "referenceLocator":
                    "pkg:generic/cpython-embed@3.12.10",
            }],
            "checksums": [{
                "algorithm": "SHA256",
                "checksumValue": "2" * 64,
            }],
        })
        document["relationships"].append({
            "spdxElementId": "SPDXRef-Package-AutoTrade",
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": "SPDXRef-Python",
        })
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError,
            "do not exactly match SBOM runtime scope",
        ):
            self._build(sbom_override=(document, raw))


    def test_orphan_package_rights_record_fails(self):
        orphan = {
            "name": "Unused.Package",
            "version": "9.9.9",
            "content_hash_sha512_base64": base64.b64encode(
                sha512(b"unused").digest()
            ).decode("ascii"),
            "license_id": "MIT",
        }
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "outside the locked graph"
        ):
            self._build(rights=RIGHTS + [orphan])

    def test_same_package_version_with_multiple_rights_hashes_fails(self):
        conflicting = dict(RIGHTS[0])
        conflicting["content_hash_sha512_base64"] = base64.b64encode(
            sha512(b"different").digest()
        ).decode("ascii")
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "multiple content hashes"
        ):
            self._build(rights=RIGHTS + [conflicting])

    def test_orphan_external_runtime_rights_record_fails(self):
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "do not exactly match SBOM runtime scope"
        ):
            self._build(external_rights=EXTERNAL_RIGHTS)

    def test_duplicate_provenance_component_name_fails(self):
        with self.assertRaisesRegex(
            ReleaseScopeMappingError, "component name is duplicated"
        ):
            self._build(
                provenance=PROVENANCE + [dict(PROVENANCE[0])]
            )

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
            reuse_documents=REUSE,
        )
        composition["runtime"]["minimum_windows_version"] = "11"
        second = build_mapping(
            composition=composition,
            sbom=sbom,
            sbom_raw=raw,
            locked_packages=LOCK,
            package_rights=RIGHTS,
            provenance_components=PROVENANCE,
            reuse_documents=REUSE,
        )
        self.assertNotEqual(
            first["mapping_digest"], second["mapping_digest"]
        )


if __name__ == "__main__":
    unittest.main()
