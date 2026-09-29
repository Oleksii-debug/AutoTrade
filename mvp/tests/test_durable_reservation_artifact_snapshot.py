from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import ReservationConflict
from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)


class DurableReservationArtifactSnapshotTests(unittest.TestCase):
    def test_terminal_release_snapshot_failure_is_pre_mutation_and_never_split_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = JournalStore(root / "journal.sqlite")
            artifacts = ArtifactStore(root / "artifacts")
            book = DurableReservationBook(
                journal,
                environment="PAPER",
                account_id="paper-account",
                resolution_artifact_store=artifacts,
            )
            book.reserve(
                command_id="reserve",
                idempotency_key="reserve",
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": "70"},
                available={"CASH:USD": "100"},
            )
            book.mark_unknown(
                command_id="unknown",
                idempotency_key="unknown",
                reservation_id="r1",
            )
            before_version = book.version
            before_reserved = book.total_reserved("CASH:USD")

            with (
                patch.object(
                    artifacts,
                    "read_authenticated_snapshot",
                    side_effect=ArtifactIntegrityError("simulated authenticated snapshot failure"),
                ) as snapshot_read,
                patch.object(
                    artifacts,
                    "load_manifest",
                    side_effect=AssertionError("legacy split manifest read must not be used"),
                ) as legacy_manifest,
                patch.object(
                    artifacts,
                    "read_bytes",
                    side_effect=AssertionError("legacy split body read must not be used"),
                ) as legacy_body,
            ):
                with self.assertRaisesRegex(
                    ReservationConflict,
                    "resolution evidence verification failed",
                ):
                    book.mark_terminal(
                        command_id="terminal",
                        idempotency_key="terminal",
                        reservation_id="r1",
                        outcome="PROVEN_ABSENT",
                        provider="SIMULATED",
                        attempt_id="attempt-r1",
                        resolution_evidence=(
                            "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                            + "a" * 64
                        ),
                    )

            snapshot_read.assert_called_once_with(
                "11111111-1111-4111-8111-111111111111"
            )
            legacy_manifest.assert_not_called()
            legacy_body.assert_not_called()
            self.assertEqual(book.version, before_version)
            self.assertEqual(book.get("r1").state, "UNKNOWN")
            self.assertEqual(book.total_reserved("CASH:USD"), before_reserved)
            self.assertEqual(before_reserved, Decimal("70"))


if __name__ == "__main__":
    unittest.main()
