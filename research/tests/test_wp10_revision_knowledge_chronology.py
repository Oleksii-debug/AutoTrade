from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

import autotrade_research.data.vintages as vintages_module
from autotrade_research.data.vintages import (
    HistoricalConflict,
    HistoricalDataError,
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
) -> dict:
    source_at = BASE
    available_at = BASE + timedelta(minutes=available_minutes)
    ingested_at = BASE + timedelta(minutes=ingested_minutes)
    result = {
        "event_id": EVENT_ID,
        "instrument_version": "instrument:AAA:v1",
        "adapter_version": "research-fixture@1",
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
        cases = ({"kind": "TRADE"},)
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

    def test_revision_cannot_change_adapter_build(self):
        lower = _event(1, available_minutes=1, ingested_minutes=3)
        higher = _event(2, available_minutes=2, ingested_minutes=4)
        higher["adapter_version"] = "research-fixture@2"
        with self.assertRaisesRegex(HistoricalConflict, "changed source identity metadata"):
            causal_market_event_history([lower, higher], BASE + timedelta(minutes=5))

    def test_adapter_build_must_be_canonical(self):
        row = _event(1, available_minutes=1, ingested_minutes=3)
        row["adapter_version"] = " research-fixture@1 "
        with self.assertRaisesRegex(HistoricalDataError, "adapter_version must be"):
            causal_market_event_history([row], BASE + timedelta(minutes=4))

    def test_revision_may_have_a_later_provider_source_sequence(self):
        lower = _event(1, available_minutes=1, ingested_minutes=3, source_sequence=10)
        higher = _event(2, available_minutes=2, ingested_minutes=4, source_sequence=11)

        history = causal_market_event_history(
            [higher, lower],
            BASE + timedelta(minutes=5),
        )

        self.assertEqual([row["source_sequence"] for row in history], ["10", "11"])

    def test_research_market_event_fields_match_canonical_schema(self):
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "jsonschema"
            / "market.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        definition = schema["$defs"]["MarketEvent"]
        self.assertFalse(definition["additionalProperties"])
        self.assertEqual(
            vintages_module._MARKET_EVENT_CANONICAL_FIELDS,
            frozenset(definition["properties"]),
        )
        self.assertEqual(
            vintages_module._MARKET_EVENT_REQUIRED_FIELDS,
            frozenset(definition["required"]),
        )
        self.assertEqual(
            vintages_module._MARKET_EVENT_KINDS,
            frozenset(definition["properties"]["kind"]["enum"]),
        )

    def test_zero_revision_is_valid_canonical_market_sequence(self):
        row = _event(0, available_minutes=1, ingested_minutes=3)
        history = causal_market_event_history([row], BASE + timedelta(minutes=4))
        self.assertEqual([item["revision"] for item in history], ["0"])

    def test_market_event_schema_semantics_fail_closed(self):
        cases = []
        invalid_kind = _event(1, available_minutes=1, ingested_minutes=3)
        invalid_kind["kind"] = "CUSTOM"
        cases.append((invalid_kind, "kind is not canonical"))

        invalid_payload = _event(1, available_minutes=1, ingested_minutes=3)
        invalid_payload["payload"] = []
        cases.append((invalid_payload, "payload must be an object"))

        numeric_revision = _event(1, available_minutes=1, ingested_minutes=3)
        numeric_revision["revision"] = 1
        cases.append((numeric_revision, "revision must be a canonical string Sequence"))

        numeric_sequence = _event(1, available_minutes=1, ingested_minutes=3)
        numeric_sequence["source_sequence"] = 1
        cases.append((numeric_sequence, "source_sequence must be a canonical string Sequence"))

        duplicate_flags = _event(1, available_minutes=1, ingested_minutes=3)
        duplicate_flags["quality_flags"] = ["STALE", "STALE"]
        cases.append((duplicate_flags, "quality_flags"))

        empty_flag = _event(1, available_minutes=1, ingested_minutes=3)
        empty_flag["quality_flags"] = [""]
        cases.append((empty_flag, "quality_flags"))

        extra_evidence = _event(1, available_minutes=1, ingested_minutes=3)
        extra_evidence["raw_evidence_ref"]["research_only"] = True
        cases.append((extra_evidence, "canonical EvidenceRef contract"))

        for row, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(HistoricalDataError, message):
                    causal_market_event_history(
                        [row],
                        BASE + timedelta(minutes=4),
                    )

    def test_noncanonical_market_event_field_cannot_become_research_authority(self):
        row = _event(1, available_minutes=1, ingested_minutes=3)
        row["stream_generation"] = "1"
        with self.assertRaisesRegex(
            HistoricalDataError,
            "canonical MarketEvent contract",
        ):
            causal_market_event_history([row], BASE + timedelta(minutes=4))

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
