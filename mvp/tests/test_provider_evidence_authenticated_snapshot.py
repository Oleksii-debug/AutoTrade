from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4
import gc
import subprocess
import sys
import unittest
import weakref

from autotrade_runtime.artifacts import ArtifactStore
import autotrade_runtime.artifacts._root_authority as root_authority

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

    def test_collected_projection_releases_selected_authorities_without_successor(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/artifacts")
            gc.collect()
            reader_capabilities_before = set(root_authority._READER_CAPABILITIES)
            book = durable(store, artifacts)
            reader_capabilities_with_book = set(root_authority._READER_CAPABILITIES)
            added_reader_capabilities = (
                reader_capabilities_with_book - reader_capabilities_before
            )
            self.assertEqual(
                reader_capabilities_before - reader_capabilities_with_book,
                set(),
            )
            self.assertEqual(len(added_reader_capabilities), 1)

            book_ref = weakref.ref(book)
            store_ref = weakref.ref(store)
            artifacts_ref = weakref.ref(artifacts)

            del book
            del store
            del artifacts
            gc.collect()

            self.assertIsNone(book_ref())
            self.assertIsNone(store_ref())
            self.assertIsNone(artifacts_ref())
            self.assertEqual(
                set(root_authority._READER_CAPABILITIES),
                reader_capabilities_before,
            )

    def test_order_projection_import_is_hermetic_without_research_package(self):
        root = Path(__file__).resolve().parents[2]
        script = r"""
import importlib.abc
import sys


class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "research" or fullname.startswith("research."):
            raise ImportError("research package is unavailable in installed runtime")
        return None


sys.meta_path.insert(0, BlockResearch())
import autotrade_runtime.artifacts as runtime_artifacts
import mvp.autotrade_mvp.durable_order_projection as projection

assert projection.ArtifactStore is runtime_artifacts.ArtifactStore
leaked = sorted(
    name for name in sys.modules
    if name == "research" or name.startswith("research.")
)
if leaked:
    raise AssertionError(f"durable order projection imported research package: {leaked}")
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                f"import sys; sys.path.insert(0, {str(root)!r})\n" + script,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
            book, artifacts, request, ref = prepared_book(directory)
            manifest, _artifact_bytes = artifacts.read_authenticated_snapshot(
                ref["artifact_id"]
            )

            def forged_snapshot(_book, artifact_id):
                self.assertEqual(artifact_id, ref["artifact_id"])
                return manifest, b"forged provider bytes"

            with self.assertRaisesRegex(
                OrderProjectionConflict,
                "digest differs from immutable artifact",
            ):
                book._verify_provider_evidence(
                    operation="ACKNOWLEDGE",
                    request=request,
                    evidence_refs=[ref],
                    committed_at=T1,
                    _read_authenticated_snapshot=forged_snapshot,
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

    def test_trusted_reader_factory_rebinding_after_construction_does_not_redirect(self):
        with TemporaryDirectory() as directory:
            book, _artifacts, _request, ref = prepared_book(directory)
            with patch.object(
                durable_order_projection_module,
                "trusted_authenticated_reader",
                side_effect=AssertionError("rebound reader factory must not run"),
            ):
                result = book.acknowledge(
                    event_key="ack-reader-factory-rebind",
                    client_order_id="c1",
                    provider_order_id="provider-order-1",
                    status="ACCEPTED",
                    committed_at=T1,
                    evidence_refs=[ref],
                )
            self.assertEqual(result.snapshot.state, "WORKING")

    def test_instance_reader_shadow_cannot_redirect_pinned_private_reader(self):
        with TemporaryDirectory() as directory:
            book, artifacts, _request, ref = prepared_book(directory)
            artifacts.read_authenticated_snapshot = lambda _artifact_id: (
                {"sha256": ref["sha256"], "metadata": {}},
                b"forged provider bytes",
            )
            result = book.acknowledge(
                event_key="ack-instance-shadow",
                client_order_id="c1",
                provider_order_id="provider-order-1",
                status="ACCEPTED",
                committed_at=T1,
                evidence_refs=[ref],
            )
            self.assertEqual(result.snapshot.state, "WORKING")

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
