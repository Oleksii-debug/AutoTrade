from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Callable

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_provider_qualification import (
    ProviderQualificationError,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_provider_qualification_authority import (
    _ProjectionOnlyRegistry,
    _issued,
)


class _AfterHistoryRegistry(_ProjectionOnlyRegistry):
    """Causal test harness that runs one competing writer after a frozen read cut."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.after_history: Callable[[], None] | None = None

    def _history(self, *, journal_sequence_cut: int | None = None):
        history = super()._history(journal_sequence_cut=journal_sequence_cut)
        callback = self.after_history
        if callback is not None:
            self.after_history = None
            callback()
        return history


class ProviderQualificationSupersessionCutTests(unittest.TestCase):
    def test_competing_supersession_after_validation_cannot_append_from_stale_cut(self):
        q1, r1, p1 = _issued(ordinal=60, campaign_version=1)
        q2, r2, p2 = _issued(
            ordinal=61,
            campaign_version=2,
            supersedes=q1.qualification_id,
            route_parser="BYBIT_ORDER_V5_JSON_V2",
        )
        q3, r3, p3 = _issued(
            ordinal=62,
            campaign_version=3,
            supersedes=q1.qualification_id,
            route_parser="BYBIT_ORDER_V5_JSON_V3",
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "journal.db"
            evidence_root = root / "evidence"
            registry = _AfterHistoryRegistry(
                JournalStore(database),
                evidence_store=ArtifactStore(evidence_root),
                evidence_root=evidence_root,
            )
            competing = _ProjectionOnlyRegistry(
                JournalStore(database),
                evidence_store=ArtifactStore(evidence_root),
                evidence_root=evidence_root,
            )

            for record, receipt, protocol in (
                (q1, r1, p1),
                (q2, r2, p2),
                (q3, r3, p3),
            ):
                registry._append_accepted(
                    protocol_key=protocol.key,
                    record=record,
                    receipt=receipt,
                )

            registry.after_history = lambda: competing._append_supersession(
                old_id=q1.qualification_id,
                new_id=q3.qualification_id,
            )

            with self.assertRaisesRegex(
                ProviderQualificationError,
                "supersession changed concurrently",
            ):
                registry._append_supersession(
                    old_id=q1.qualification_id,
                    new_id=q2.qualification_id,
                )

            history = competing._history()
            self.assertEqual(
                history.superseded,
                {q1.qualification_id: q3.qualification_id},
            )
            supersession_events = [
                event
                for event in competing.store.load_events_by_aggregate_type(
                    "provider_qualification"
                )
                if event["event_type"] == "ProviderQualificationSuperseded.v1"
            ]
            self.assertEqual(len(supersession_events), 1)
            self.assertEqual(
                supersession_events[0]["payload"]["new_qualification_id"],
                q3.qualification_id,
            )


if __name__ == "__main__":
    unittest.main()
