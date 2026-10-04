from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityError,
    EvidenceVerification,
    artifact_store_evidence_verifier,
    derive_capability_snapshot,
)
from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
)


NOW = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
SNAPSHOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SOURCES = ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
PRODUCER_TYPES = {
    "DOCUMENTED": "PROVIDER_DOCUMENTATION",
    "API": "PROVIDER_API",
    "ACCOUNT": "ACCOUNT_CAPABILITY",
    "INSTRUMENT": "INSTRUMENT_CAPABILITY",
}
ISSUER_HEX = {
    "DOCUMENTED": "1",
    "API": "2",
    "ACCOUNT": "3",
    "INSTRUMENT": "4",
}


def _issuer_ref(source: str) -> str:
    return f"fixture-{source.lower()}:sha256:" + ISSUER_HEX[source] * 64


def _issuer_sha256(source: str) -> str:
    return "sha256:" + ISSUER_HEX[source] * 64


def _trusted_issuer_verifiers():
    def make(expected_source: str):
        def verify(claim, issuer_ref, issuer_sha256):
            if claim.source != expected_source:
                return EvidenceVerification(
                    valid=False,
                    conflicted=True,
                    reason="issuer source mismatch",
                )
            if (
                issuer_ref != _issuer_ref(expected_source)
                or issuer_sha256 != _issuer_sha256(expected_source)
            ):
                return EvidenceVerification(
                    valid=False,
                    conflicted=True,
                    reason="issuer identity mismatch",
                )
            return EvidenceVerification(valid=True)

        return verify

    return {source: make(source) for source in SOURCES}


def _claim(
    source: str,
    evidence_ref: dict[str, object],
    *,
    expires_at=None,
) -> CapabilityClaim:
    return CapabilityClaim(
        source=source,
        provider_id="simulated",
        account_id="paper-account",
        entity_id="entity-1",
        environment="PAPER",
        instrument_version="instrument-v1",
        observed_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or NOW + timedelta(minutes=10),
        supported_order_types=frozenset({"LIMIT"}),
        time_in_force=frozenset({"DAY"}),
        permission_scopes=frozenset({"ORDER.READ", "ORDER.WRITE"}),
        position_mode="NET",
        native_protection=frozenset({"STOP_LOSS"}),
        rate_limit_policy_id="sim-v1",
        data_entitlements=frozenset({"QUOTE", "TRADE"}),
        evidence_ref=evidence_ref,
    )


def _publish(
    store: ArtifactStore,
    source: str,
    *,
    account_id="paper-account",
    provider_environment="PAPER",
) -> dict[str, object]:
    artifact_id = str(uuid4())
    observed_at = "2026-09-24T15:59:00Z"
    source_uri = f"https://evidence.invalid/{source.lower()}"
    rights_id = "capability-evidence-test"
    payload = json.dumps(
        {
            "source": source,
            "provider_id": "simulated",
            "account_id": account_id,
            "instrument_version": "instrument-v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=payload,
        media_type="application/json",
        rights={"storage": True, "export": False, "rights_id": rights_id},
        source_refs=[source_uri],
        metadata={
            "artifact_kind": "CAPABILITY_EVIDENCE",
            "schema_version": 1,
            "capability_source": source,
            "producer_type": PRODUCER_TYPES[source],
            "producer_id": f"fixture-{source.lower()}",
            "evidence_version": "1",
            "provider_id": "simulated",
            "account_id": account_id,
            "entity_id": "entity-1",
            "environment": "PAPER",
            "provider_environment": provider_environment,
            "instrument_version": "instrument-v1",
            "observed_at": observed_at,
            "issuer_ref": _issuer_ref(source),
            "issuer_sha256": _issuer_sha256(source),
        },
    )
    return {
        "artifact_id": artifact_id,
        "sha256": manifest["sha256"],
        "observed_at": observed_at,
        "source_uri": source_uri,
        "rights_id": rights_id,
        "issuer_ref": _issuer_ref(source),
        "issuer_sha256": _issuer_sha256(source),
    }


def _verifier(directory, store, *, issuers=True):
    return artifact_store_evidence_verifier(
        evidence_root=directory,
        publication_store=store,
        issuer_verifiers=_trusted_issuer_verifiers() if issuers else None,
    )


class CapabilityArtifactEvidenceTests(unittest.TestCase):
    def test_evidence_verdict_cannot_use_truthy_non_boolean_authority(self):
        with self.assertRaisesRegex(CapabilityError, "must be boolean"):
            EvidenceVerification(valid="yes")
        with self.assertRaisesRegex(CapabilityError, "requires a reason"):
            EvidenceVerification(valid=False)
        with self.assertRaisesRegex(CapabilityError, "reason must be text"):
            EvidenceVerification(valid=False, reason=123)

    def test_trusted_root_is_mandatory_and_store_cannot_select_it(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            with self.assertRaisesRegex(TypeError, "evidence_root is required"):
                artifact_store_evidence_verifier(store)

    def test_publication_store_must_match_independent_root_generation(self):
        with TemporaryDirectory() as trusted_directory:
            with TemporaryDirectory() as other_directory:
                trusted_store = ArtifactStore(trusted_directory)
                other_store = ArtifactStore(other_directory)
                _publish(other_store, "API")
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "does not match trusted artifact root",
                ):
                    artifact_store_evidence_verifier(
                        evidence_root=trusted_directory,
                        publication_store=other_store,
                        issuer_verifiers=_trusted_issuer_verifiers(),
                    )
                self.assertIsInstance(trusted_store, ArtifactStore)

    def test_publication_store_subclass_is_rejected_before_verification(self):
        class ForgedStore(ArtifactStore):
            pass

        with TemporaryDirectory() as directory:
            canonical = ArtifactStore(directory)
            _publish(canonical, "API")
            forged = ForgedStore(directory)
            with self.assertRaisesRegex(
                TypeError,
                "publication_store must be the canonical ArtifactStore",
            ):
                artifact_store_evidence_verifier(
                    evidence_root=directory,
                    publication_store=forged,
                    issuer_verifiers=_trusted_issuer_verifiers(),
                )

    def test_caller_store_poisoning_before_capture_cannot_select_reader(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}

            def forbidden(*_args, **_kwargs):
                raise AssertionError("caller store authority must not be used")

            store.load_manifest = forbidden
            store.read_bytes = forbidden
            store._read_verified_object_bytes = forbidden
            store._decode_manifest_bytes = forbidden
            store.root = Path(directory) / "caller-redirect-before-capture"

            verifier = _verifier(directory, store)
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=tuple(_claim(source, refs[source]) for source in SOURCES),
                observed_at=NOW,
                evidence_verifier=verifier,
            )
            self.assertEqual(snapshot.status, "VERIFIED")
            self.assertEqual(snapshot.sources, frozenset(SOURCES))

    def test_caller_store_methods_cannot_redirect_captured_trusted_reader(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}
            verifier = _verifier(directory, store)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("caller store method must not be used")

            store.load_manifest = forbidden
            store.read_bytes = forbidden
            store._read_verified_object_bytes = forbidden
            store._decode_manifest_bytes = forbidden
            store.root = Path(directory) / "caller-redirected-root"

            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=tuple(_claim(source, refs[source]) for source in SOURCES),
                observed_at=NOW,
                evidence_verifier=verifier,
            )
            self.assertEqual(snapshot.status, "VERIFIED")
            self.assertEqual(snapshot.sources, frozenset(SOURCES))

    def test_each_claim_uses_exactly_one_authenticated_snapshot_read(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            evidence_ref = _publish(store, "API")
            claim = _claim("API", evidence_ref)
            artifact_id = str(evidence_ref["artifact_id"])
            held_snapshot = store.read_authenticated_snapshot(artifact_id)
            calls: list[str] = []

            def counted_reader(requested_artifact_id: str):
                calls.append(requested_artifact_id)
                return held_snapshot

            with patch(
                "mvp.autotrade_mvp.capabilities.trusted_authenticated_reader",
                return_value=counted_reader,
            ) as reader_factory:
                verifier = artifact_store_evidence_verifier(
                    evidence_root=directory,
                    publication_store=store,
                    issuer_verifiers=_trusted_issuer_verifiers(),
                )
                result = verifier(claim)

            self.assertTrue(result.valid)
            self.assertEqual(calls, [artifact_id])
            reader_factory.assert_called_once_with(
                directory,
                publication_store=store,
            )

    def test_syntactically_valid_but_missing_artifacts_never_verify(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            claims = tuple(
                _claim(
                    source,
                    {
                        "artifact_id": str(uuid4()),
                        "sha256": "sha256:" + "a" * 64,
                        "observed_at": "2026-09-24T15:59:00Z",
                    },
                )
                for source in SOURCES
            )
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=claims,
                observed_at=NOW,
                evidence_verifier=_verifier(directory, store),
            )
            self.assertEqual(snapshot.status, "UNKNOWN")
            self.assertEqual(snapshot.sources, frozenset())
            self.assertEqual(snapshot.evidence, ())

    def test_generic_artifact_metadata_cannot_self_authorize_verified_snapshot(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=tuple(_claim(source, refs[source]) for source in SOURCES),
                observed_at=NOW,
                evidence_verifier=_verifier(directory, store, issuers=False),
            )
            self.assertEqual(snapshot.status, "UNKNOWN")
            self.assertEqual(snapshot.sources, frozenset())
            self.assertEqual(snapshot.evidence, ())

    def test_existing_artifact_bytes_corruption_is_conflicted(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}
            digest = str(refs["API"]["sha256"]).removeprefix("sha256:")
            object_path = Path(directory) / "objects" / "sha256" / digest[:2] / digest
            object_path.write_bytes(b"corrupted-after-publish")
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=tuple(_claim(source, refs[source]) for source in SOURCES),
                observed_at=NOW,
                evidence_verifier=_verifier(directory, store),
            )
            self.assertEqual(snapshot.status, "CONFLICTED")
            self.assertNotIn("API", snapshot.sources)

    def test_existing_artifact_digest_mismatch_is_conflicted(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}
            refs["API"] = dict(refs["API"])
            refs["API"]["sha256"] = "sha256:" + "b" * 64
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=tuple(_claim(source, refs[source]) for source in SOURCES),
                observed_at=NOW,
                evidence_verifier=_verifier(directory, store, issuers=False),
            )
            self.assertEqual(snapshot.status, "CONFLICTED")

    def test_wrong_account_identity_artifact_is_conflicted(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}
            refs["ACCOUNT"] = _publish(store, "ACCOUNT", account_id="other-account")
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=tuple(_claim(source, refs[source]) for source in SOURCES),
                observed_at=NOW,
                evidence_verifier=_verifier(directory, store, issuers=False),
            )
            self.assertEqual(snapshot.status, "CONFLICTED")

    def test_wrong_provider_environment_artifact_is_conflicted(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}
            refs["API"] = _publish(store, "API", provider_environment="OTHER-DOMAIN")
            snapshot = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=tuple(_claim(source, refs[source]) for source in SOURCES),
                observed_at=NOW,
                evidence_verifier=_verifier(directory, store),
            )
            self.assertEqual(snapshot.status, "CONFLICTED")
            self.assertNotIn("API", snapshot.sources)

    def test_exact_immutable_artifacts_verify_only_inside_claim_window(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            refs = {source: _publish(store, source) for source in SOURCES}
            claims = tuple(_claim(source, refs[source]) for source in SOURCES)
            verifier = _verifier(directory, store)
            current = derive_capability_snapshot(
                snapshot_id=SNAPSHOT,
                claims=claims,
                observed_at=NOW,
                evidence_verifier=verifier,
            )
            self.assertEqual(current.status, "VERIFIED")
            self.assertEqual(current.sources, frozenset(SOURCES))
            self.assertEqual(len(current.evidence), 4)
            self.assertFalse(
                current.admits(
                    at=NOW,
                    order_type="LIMIT",
                    time_in_force="DAY",
                    permission_scope="ORDER.WRITE",
                )
            )

            expired = derive_capability_snapshot(
                snapshot_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                claims=claims,
                observed_at=NOW + timedelta(minutes=11),
                evidence_verifier=verifier,
            )
            self.assertEqual(expired.status, "EXPIRED")


if __name__ == "__main__":
    unittest.main()
