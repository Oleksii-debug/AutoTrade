from datetime import datetime, timedelta, timezone
import unittest

from autotrade_research.data.vintages import (
    HistoricalConflict,
    causal_market_event_history,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
EVENT_ID = "00000001-0000-4000-8000-000000000001"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event(
    revision: int,
    *,
    available_minutes: int,
    ingested_minutes: int,
    kind: str = "BAR",
    source_sequence: int | None = 10,
    stream_generation: int | None = 1,
) -> dict:
    source_at = BASE
    available_at = BASE + timedelta(minutes=available_minutes)
    ingested_at = BASE + timedelta(minutes=ingested_minutes)
    result = {
        "event_id": EVENT_ID,
        "instrument_version": "instrument:AAA:v1",
        "kind": kind,
        "source_event_at": _iso(source_at),
        "available_at": _iso(available_at),
        "ingested_at": _iso(ingested_at),
        "revision": str(revision),
        "availability_basis": "provider-history",
        "quality_flags": [],
        "payload": {"close": str(100 + revision)},
        "raw_evidence_ref": {
            "artifact_id": f"0000000{revision}-0000-4000-8000-00000000000{revision}",
            "sha256": "sha256:" + str(revision) * 64,
            "observed_at": _iso(ingested_at),
            "rights_id": "research-fixture",
        },
    }
    if source_sequence is not None:
        result["source_sequence"] = str(source_sequence)
    if stream_generation is not None:
        result["stream_generation"] = str(stream_generation)
    return result


class RevisionKnowledgeChronologyTests(unittest.TestCase):
    def test_higher_revision_cannot_become_known_before_lower_revision(self):
        lower = _event(1, available_minutes=1, ingested_minutes=30)
        higher = _event(2, available_minutes=2, ingested_minutes=3)

        with self.assertRaisesRegex(
            HistoricalConflict,
            "higher event revision must have later effective knowledge time",
        ):
            causal_market_event_history(
                [lower, higher],
                BASE + timedelta(minutes=31),
            )

    def test_distinct_revisions_cannot_share_effective_knowledge_time(self):
        lower = _event(1, available_minutes=1, ingested_minutes=3)
        higher = _event(2, available_minutes=2, ingested_minutes=3)

        with self.assertRaisesRegex(
            HistoricalConflict,
            "higher event revision must have later effective knowledge time",
        ):
            causal_market_event_history(
                [lower, higher],
                BASE + timedelta(minutes=4),
            )

    def test_revision_cannot_change_stable_source_identity_metadata(self):
        cases = (
            {"kind": "TRADE"},
            {"source_sequence": 11},
            {"stream_generation": 2},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                lower = _event(
                    1,
                    available_minutes=1,
                    ingested_minutes=3,
                )
                higher = _event(
                    2,
                    available_minutes=2,
                    ingested_minutes=4,
                    **changed,
                )
                with self.assertRaisesRegex(
                    HistoricalConflict,
                    "event revision changed source identity metadata",
                ):
                    causal_market_event_history(
                        [lower, higher],
                        BASE + timedelta(minutes=5),
                    )

    def test_strictly_later_revision_knowledge_time_is_accepted(self):
        lower = _event(1, available_minutes=1, ingested_minutes=3)
        higher = _event(2, available_minutes=2, ingested_minutes=4)

        history = causal_market_event_history(
            [higher, lower],
            BASE + timedelta(minutes=5),
        )

        self.assertEqual([row["revision"] for row in history], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
