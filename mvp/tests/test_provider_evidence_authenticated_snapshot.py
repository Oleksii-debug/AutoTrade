from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
import unittest

import research.autotrade_research.artifacts.store as artifact_store_module
from research.autotrade_research.artifacts.store import ArtifactStore

import mvp.autotrade_mvp.durable_order_projection as durable_order_projection_module
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.order_projection import OrderProjectionConflict
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest


T0 = "2026-10-04T00:00:00Z"
T1 = "2026-10-04T00:00:01Z"


def durable(store, artifacts):
    return DurableOrderBookProjection(
        store,
        provider_id="PROVIDER-A",
        account_id="acct-1",
        environment="PAPER",
        host_id="host-1",
        owner_epoch="1",
        evidence_artifact_store=artifacts,
    )


def provider_evidence(artifact_store, *, request):
    artifact_id = str(uuid4())
    source_uri = "https://provider.example.test/evidence"
    payload = canonical_json(
        {
            "operation": "ACKNOWLEDGE",
            "request": request,
            "observed_at": T1,
        }
    ).encode("utf-8")
    manifest = artifact_store.publish_bytes(
        artifact_id=artifact_id,
        data=payload,
        media_type="application/json",
        rights={"storage": True, "export": False},
        source_refs=[source_uri],
        metadata={
            "provider_id": "PROVIDER-A",
            "account_id": "acct-1",
            "environment": "PAPER",
            "order_operation": "ACKNOWLEDGE",
            "request_hash": payload_digest(request),
            "observed_at": T1,
            "rights_id": "provider-test-evidence",
        },
    )
    return {
        "artifact_id": artifact_id,
        "sha256": manifest["sha256"],
        "source_uri": source_uri,
        "observed_at": T1,
        "rights_id": "provider-test-evidence",
    }


def prepared_book(directory):
    store = JournalStore(f"{directory}/journal.sqlite3")
    artifacts = ArtifactStore(f"{directory}/artifacts")
    book = durable(store, artifacts)
    book.create_order(
        event_key="create-1",
        client_order_id="c1",
        instrument="ABC",
        side="BUY",
        requested_quantity="1",
        committed_at=T0,
    )
    request = {
        "client_order_id": "c1",
        "provider_order_id": "provider-order-1",
        "status": "ACCEPTED",
        "attempt_id": None,
    }
    return book, artifacts, request, provider_evidence(artifacts, request=request)


class ProviderEvidenceAuthenticatedSnapshotTests(unittest.TestCase):
    def test_artifact_store_subclass_is_rejected(self):
        class DerivedArtifactStore(ArtifactStore):
            pass

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = DerivedArtifactStore(f"{directory}/artifacts")
            with self.assertRaisesRegex(TypeError, "exact canonical ArtifactStore"):
                durable(store, artifacts)

    def test_provider_evidence_uses_one_authenticated_snapshot(self):
        with TemporaryDirectory() as directory:
            book, artifacts, _request, ref = prepared_book(directory)
            with (
                patch.object(
                    ArtifactStore,
                    "load_manifest",
                    side_effect=AssertionError("split manifest read must not run"),
                ),
                patch.object(
                    ArtifactStore,
                    "read_bytes",
                    side_effect=AssertionError("split object read must not run"),
                ),
            ):
                result = book.acknowledge(
                    event_key="ack-1",
                    client_order_id="c1",
                    provider_order_id="provider-order-1",
                    status="ACCEPTED",
                    committed_at=T1,
                    evidence_refs=[ref],
                )
            self.assertEqual(result.snapshot.state, "WORKING")

    def test_authenticated_bytes_are_independently_rehashed(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            with patch.object(
                ArtifactStore,
                "_read_verified_object_bytes",
                return_value=b"forged provider bytes",
            ):
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "digest differs from immutable artifact",
                ):
                    book.acknowledge(
                        event_key="ack-rehash",
                        client_order_id="c1",
                        provider_order_id="provider-order-1",
                        status="ACCEPTED",
                        committed_at=T1,
                        evidence_refs=[ref],
                    )

    def test_artifact_store_namespace_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            book, artifacts, _request, ref = prepared_book(directory)
            artifacts.root = Path(directory) / "different-artifacts"
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "namespace authority changed",
            ):
                book.acknowledge(
                    event_key="ack-retarget",
                    client_order_id="c1",
                    provider_order_id="provider-order-1",
                    status="ACCEPTED",
                    committed_at=T1,
                    evidence_refs=[ref],
                )

    def test_manifest_decoder_rebinding_fails_closed_before_redirect(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            with patch.object(
                ArtifactStore,
                "_decode_manifest_bytes",
                side_effect=AssertionError("forged manifest decoder executed"),
            ):
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "reader graph changed",
                ):
                    book.acknowledge(
                        event_key="ack-decoder-rebind",
                        client_order_id="c1",
                        provider_order_id="provider-order-1",
                        status="ACCEPTED",
                        committed_at=T1,
                        evidence_refs=[ref],
                    )

    def test_manifest_integrity_rebinding_fails_closed_before_redirect(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            with patch.object(
                artifact_store_module,
                "_verify_manifest_integrity",
                side_effect=AssertionError("forged integrity verifier executed"),
            ):
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "reader graph changed",
                ):
                    book.acknowledge(
                        event_key="ack-integrity-rebind",
                        client_order_id="c1",
                        provider_order_id="provider-order-1",
                        status="ACCEPTED",
                        committed_at=T1,
                        evidence_refs=[ref],
                    )

    def test_manifest_decoder_code_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            decoder = ArtifactStore._decode_manifest_bytes
            original_code = decoder.__code__

            def forged_decoder(self, manifest_path, raw_bytes):
                del self, manifest_path, raw_bytes
                raise AssertionError("forged decoder executed")

            try:
                decoder.__code__ = forged_decoder.__code__
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "reader graph changed",
                ):
                    book.acknowledge(
                        event_key="ack-decoder-code",
                        client_order_id="c1",
                        provider_order_id="provider-order-1",
                        status="ACCEPTED",
                        committed_at=T1,
                        evidence_refs=[ref],
                    )
            finally:
                decoder.__code__ = original_code

    def test_retained_authenticated_reader_code_retarget_fails_closed(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            reader = ArtifactStore.read_authenticated_snapshot
            original_code = reader.__code__

            def forged_reader(self, artifact_id):
                del self, artifact_id
                raise AssertionError("forged provider reader executed")

            try:
                reader.__code__ = forged_reader.__code__
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "snapshot reader authority changed",
                ):
                    book.acknowledge(
                        event_key="ack-reader-code",
                        client_order_id="c1",
                        provider_order_id="provider-order-1",
                        status="ACCEPTED",
                        committed_at=T1,
                        evidence_refs=[ref],
                    )
            finally:
                reader.__code__ = original_code

    def test_instance_reader_shadow_fails_closed_before_redirect(self):
        with TemporaryDirectory() as directory:
            book, artifacts, _request, ref = prepared_book(directory)
            artifacts.read_authenticated_snapshot = lambda _artifact_id: (
                {"sha256": ref["sha256"], "metadata": {}},
                b"forged provider bytes",
            )
            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "not resolvable and intact",
            ):
                book.acknowledge(
                    event_key="ack-instance-shadow",
                    client_order_id="c1",
                    provider_order_id="provider-order-1",
                    status="ACCEPTED",
                    committed_at=T1,
                    evidence_refs=[ref],
                )

    def test_module_reader_rebinding_does_not_redirect_captured_authority(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            with patch.object(
                durable_order_projection_module,
                "_read_authenticated_provider_evidence",
                side_effect=AssertionError("rebound module reader must not run"),
            ):
                result = book.acknowledge(
                    event_key="ack-module-rebind",
                    client_order_id="c1",
                    provider_order_id="provider-order-1",
                    status="ACCEPTED",
                    committed_at=T1,
                    evidence_refs=[ref],
                )
            self.assertEqual(result.snapshot.state, "WORKING")

    def test_public_reader_rebinding_does_not_redirect_captured_authority(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                side_effect=AssertionError("rebound public reader must not run"),
            ):
                result = book.acknowledge(
                    event_key="ack-public-rebind",
                    client_order_id="c1",
                    provider_order_id="provider-order-1",
                    status="ACCEPTED",
                    committed_at=T1,
                    evidence_refs=[ref],
                )
            self.assertEqual(result.snapshot.state, "WORKING")


if __name__ == "__main__":
    unittest.main()
