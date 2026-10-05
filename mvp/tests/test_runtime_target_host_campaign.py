from __future__ import annotations

import json
import unittest

from mvp.autotrade_mvp.performance_qualification import RuntimeLoadObservation
from mvp.autotrade_mvp.runtime_load_campaign import (
    RuntimeLoadCampaignEvidence,
    serialize_runtime_load_campaign_evidence,
)
from mvp.autotrade_mvp.runtime_target_host_campaign import (
    COLLECTOR_ID,
    COLLECTOR_VERSION,
    ParsedRuntimeTargetHostCampaign,
    RuntimeTargetHostCampaignError,
)
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint


SOURCE = "a" * 40
SPEC = "sha256:" + ("b" * 64)
CONFIG = "sha256:" + ("c" * 64)
IDENTITY = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.12.11",
    "cpu_count": 8,
}
HOST = host_identity_fingerprint(IDENTITY)


def _evidence() -> RuntimeLoadCampaignEvidence:
    observation = RuntimeLoadObservation.create(
        scenario_id="target-host-primary",
        spec_digest=SPEC,
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        expected_financial_events=2,
        recovered_financial_events=2,
        financial_latency_us=(100, 200),
        financial_staleness_us=(),
        research_interference_us=(),
        recovered_financial_event_ids=("financial-1", "financial-2"),
        financial_latency_event_ids=("financial-1", "financial-2"),
        financial_staleness_event_ids=(),
        reconnect_backlog_remaining=0,
        declared_duration_us=1_000,
        observed_duration_us=900,
    )
    return RuntimeLoadCampaignEvidence(
        observation=observation,
        journal_sequence_before=10,
        journal_sequence_after=12,
        recovered_event_ids=("financial-1", "financial-2"),
        recovered_journal_sequences=(11, 12),
        host_identity=IDENTITY,
    )


def _raw() -> bytes:
    return serialize_runtime_load_campaign_evidence(_evidence())


def _mutate_raw(mutator) -> bytes:
    value = json.loads(_raw().decode("utf-8"))
    mutator(value)
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class RuntimeTargetHostCampaignTests(unittest.TestCase):
    def test_existing_campaign_serializer_round_trips_through_strict_parser(self) -> None:
        raw = _raw()
        parsed = ParsedRuntimeTargetHostCampaign.parse(raw)
        self.assertEqual(parsed.canonical_bytes, raw)
        self.assertEqual(parsed.collector_id, COLLECTOR_ID)
        self.assertEqual(parsed.collector_version, COLLECTOR_VERSION)
        self.assertEqual(parsed.evidence.observation.host_fingerprint, HOST)
        self.assertEqual(parsed.evidence.recovered_event_ids, ("financial-1", "financial-2"))
        self.assertEqual(
            parsed.evidence.observation.recovered_financial_event_ids,
            ("financial-1", "financial-2"),
        )
        self.assertEqual(
            parsed.evidence.observation.financial_latency_event_ids,
            ("financial-1", "financial-2"),
        )

    def test_current_observation_binding_fields_are_required(self) -> None:
        def mutate(value: dict[str, object]) -> None:
            observation = dict(value["observation"])
            observation.pop("recovered_financial_event_ids")
            value["observation"] = observation

        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "observation fields are non-canonical",
        ):
            ParsedRuntimeTargetHostCampaign.parse(_mutate_raw(mutate))

    def test_recovered_observation_binding_must_match_retained_journal_cut(self) -> None:
        def mutate(value: dict[str, object]) -> None:
            observation = dict(value["observation"])
            observation["recovered_financial_event_ids"] = [
                "financial-2",
                "financial-1",
            ]
            value["observation"] = observation

        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "retained evidence is invalid",
        ):
            ParsedRuntimeTargetHostCampaign.parse(_mutate_raw(mutate))

    def test_campaign_digest_substitution_is_rejected(self) -> None:
        raw = _mutate_raw(
            lambda value: value.__setitem__("evidence_digest", "sha256:" + ("9" * 64))
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "digest or document semantics conflict",
        ):
            ParsedRuntimeTargetHostCampaign.parse(raw)

    def test_campaign_host_identity_must_match_observation(self) -> None:
        def mutate(value: dict[str, object]) -> None:
            host = dict(value["host_identity"])
            host["cpu_count"] = 16
            value["host_identity"] = host

        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "host identity conflicts with observation",
        ):
            ParsedRuntimeTargetHostCampaign.parse(_mutate_raw(mutate))

    def test_campaign_recovered_count_must_match_retained_identities(self) -> None:
        def mutate(value: dict[str, object]) -> None:
            observation = dict(value["observation"])
            observation["recovered_financial_events"] = 1
            value["observation"] = observation

        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "identities do not match observation count",
        ):
            ParsedRuntimeTargetHostCampaign.parse(_mutate_raw(mutate))

    def test_campaign_sequence_cut_must_be_ordered_and_inside_campaign(self) -> None:
        raw = _mutate_raw(
            lambda value: value.__setitem__(
                "recovered_journal_sequences",
                [12, 11],
            )
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "journal sequence cut is invalid",
        ):
            ParsedRuntimeTargetHostCampaign.parse(raw)

    def test_campaign_bytes_must_match_existing_canonical_serializer(self) -> None:
        raw = _raw()
        noncanonical = json.dumps(json.loads(raw.decode("utf-8"))).encode("utf-8")
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "bytes are not canonical retained evidence",
        ):
            ParsedRuntimeTargetHostCampaign.parse(noncanonical)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        duplicate = _raw().replace(
            b'{"evidence_digest":',
            b'{"evidence_digest":"sha256:' + (b"0" * 64) + b'","evidence_digest":',
            1,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "duplicate JSON key",
        ):
            ParsedRuntimeTargetHostCampaign.parse(duplicate)

    def test_campaign_document_size_is_bounded_before_semantic_acceptance(self) -> None:
        oversized = _raw() + (b" " * 1_000_001)
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "not valid UTF-8 JSON",
        ):
            ParsedRuntimeTargetHostCampaign.parse(oversized)

    def test_campaign_nesting_is_bounded_before_recursive_decode(self) -> None:
        deeply_nested = b'{"x":' + (b"[" * 129) + b"0" + (b"]" * 129) + b"}"
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "not valid UTF-8 JSON",
        ):
            ParsedRuntimeTargetHostCampaign.parse(deeply_nested)

    def test_campaign_integer_domain_is_bounded(self) -> None:
        huge_integer = b'{"x":' + (b"9" * 641) + b"}"
        with self.assertRaisesRegex(
            RuntimeTargetHostCampaignError,
            "not valid UTF-8 JSON",
        ):
            ParsedRuntimeTargetHostCampaign.parse(huge_integer)


if __name__ == "__main__":
    unittest.main()