from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import unittest

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
import autotrade_runtime.artifacts._root_authority as root_authority
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.order_projection import OrderProjectionConflict
from mvp.autotrade_mvp.persistence import JournalStore


class ProviderEvidenceGenerationComparisonTests(unittest.TestCase):
    def test_comparison_uses_full_retained_generation_on_all_platforms(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            publication_store = ArtifactStore(root)
            private_store = ArtifactStore(root)
            publication_pins = (101, 102, 103, 104)
            private_pins = (201, 202, 203, 204)
            with (
                patch.object(
                    root_authority,
                    "_duplicate_store_generation_pins",
                    side_effect=(publication_pins, private_pins),
                ) as duplicate_generation,
                patch.object(
                    root_authority,
                    "_pinned_generation",
                    side_effect=(
                        (1, 2, 3, 4, 5, 6, 7, 8),
                        (1, 2, 3, 4, 5, 6, 7, 9),
                    ),
                ),
                patch.object(root_authority, "_close_generation_pins") as close_pins,
            ):
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "publication store does not match trusted artifact namespace generation",
                ):
                    root_authority._assert_same_root_generation(
                        publication_store,
                        private_store,
                    )
            self.assertEqual(duplicate_generation.call_count, 2)
            self.assertEqual(
                [call.args[0] for call in close_pins.call_args_list],
                [private_pins, publication_pins],
            )

    def test_publication_bound_reader_does_not_reconstruct_artifact_store(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            publication_store = ArtifactStore(root)
            with patch.object(
                ArtifactStore,
                "__init__",
                side_effect=AssertionError(
                    "trusted reader must not reconstruct publication store"
                ),
            ):
                reader = trusted_authenticated_reader(
                    root,
                    publication_store=publication_store,
                )
            self.assertTrue(callable(reader))


@unittest.skipIf(
    sys.platform == "win32",
    "POSIX retained-directory rename regression; Windows uses handle identity",
)
class ProviderEvidenceNamespaceGenerationTests(unittest.TestCase):
    def _replace_manifest_namespace(self, root: Path) -> tuple[Path, Path]:
        configured = root / "manifests"
        retained = root / "manifests.retained-test"
        configured.rename(retained)
        configured.mkdir()
        return configured, retained

    def _restore_manifest_namespace(
        self,
        configured: Path,
        retained: Path,
    ) -> None:
        configured.rmdir()
        retained.rename(configured)

    def test_trusted_reader_rejects_child_generation_mismatch_at_bind(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            publication_store = ArtifactStore(root)
            configured, retained = self._replace_manifest_namespace(root)
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    "publication store does not match trusted artifact namespace generation",
                ):
                    trusted_authenticated_reader(
                        root,
                        publication_store=publication_store,
                    )
            finally:
                self._restore_manifest_namespace(configured, retained)

    def test_missing_child_namespace_fails_without_recreation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            publication_store = ArtifactStore(root)
            configured = root / "manifests"
            retained = root / "manifests.retained-test"
            configured.rename(retained)
            try:
                with self.assertRaises(ArtifactIntegrityError):
                    trusted_authenticated_reader(
                        root,
                        publication_store=publication_store,
                    )
                self.assertFalse(
                    configured.exists(),
                    "fail-closed bind must not recreate a missing child namespace",
                )
            finally:
                if configured.exists():
                    configured.rmdir()
                retained.rename(configured)

    def test_durable_oms_fails_closed_before_binding_replaced_child_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            publication_store = ArtifactStore(root)
            journal = JournalStore(f"{directory}/journal.sqlite3")
            configured, retained = self._replace_manifest_namespace(root)
            try:
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "provider evidence trusted reader authority is unavailable",
                ):
                    DurableOrderBookProjection(
                        journal,
                        provider_id="PROVIDER-A",
                        account_id="acct-1",
                        environment="PAPER",
                        host_id="host-1",
                        owner_epoch="1",
                        evidence_artifact_store=publication_store,
                    )
            finally:
                self._restore_manifest_namespace(configured, retained)


if __name__ == "__main__":
    unittest.main()
