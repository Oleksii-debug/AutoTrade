import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.authority_persistence import persist_authority_snapshot
from mvp.autotrade_mvp.persistence import JournalStore


class _ForgedCanonicalStore(JournalStore):
    """Hostile structural store that lies about canonical authority history."""

    def load_events(self, aggregate_type, aggregate_id):
        if aggregate_type == "authority_state" and aggregate_id == "canonical":
            return [
                {
                    "event_id": "forged-canonical-authority",
                    "event_type": "AuthorityPolicyRegistered",
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "aggregate_version": 1,
                    "payload": {},
                    "payload_hash": "sha256:" + "0" * 64,
                    "committed_at": "2026-09-25T00:00:00Z",
                }
            ]
        return super().load_events(aggregate_type, aggregate_id)


class AuthoritySnapshotWriteSealTests(unittest.TestCase):
    def test_store_override_cannot_manufacture_canonical_authority_for_fresh_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _ForgedCanonicalStore(Path(directory) / "journal.sqlite")

            with self.assertRaisesRegex(ValueError, "requires canonical authority"):
                persist_authority_snapshot(
                    store,
                    AuthorityService(),
                    authority_id="runtime-authority",
                    event_id="fresh-snapshot",
                    committed_at="2026-09-25T00:00:01Z",
                )

            self.assertEqual(
                [],
                JournalStore.load_events(
                    store,
                    "financial-authority",
                    "runtime-authority",
                ),
            )


if __name__ == "__main__":
    unittest.main()
