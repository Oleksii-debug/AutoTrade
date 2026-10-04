from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.order_projection import OrderProjectionConflict
from mvp.autotrade_mvp.persistence import JournalStore


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
