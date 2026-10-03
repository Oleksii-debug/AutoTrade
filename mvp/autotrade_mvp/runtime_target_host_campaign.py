"""Strict raw campaign evidence parser for terminal WP-65 qualification.

This module reuses the existing ``RuntimeLoadCampaignEvidence`` document and
serializer. It does not define a second load format or create measurements. Its
job is to turn retained raw bytes back into the existing evidence model, reject
noncanonical/tampered documents, and prove the campaign host identity is the same
canonical target-host identity used elsewhere by WP-65.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from .performance_qualification import RuntimeBudgetError, RuntimeLoadObservation
from .runtime_load_campaign import (
    RuntimeLoadCampaignEvidence,
    runtime_load_campaign_evidence_document,
    serialize_runtime_load_campaign_evidence,
)
from .runtime_target_host_inventory import (
    RuntimeTargetHostInventoryError,
    host_identity_fingerprint,
)


SCHEMA_VERSION = "1.0.0"
EVIDENCE_TYPE = "AUTOTRADE_RUNTIME_LOAD_CAMPAIGN"
COLLECTOR_ID = "autotrade-runtime-load-campaign"
COLLECTOR_VERSION = "1.0.0"


class RuntimeTargetHostCampaignError(ValueError):
    """Raised when retained target-host campaign evidence is not canonical."""


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign contains duplicate JSON key"
            )
        result[key] = value
    return result


def _parse_json(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostCampaignError(
            "target-host campaign must be non-empty bytes"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RuntimeTargetHostCampaignError(
                    f"target-host campaign contains invalid JSON constant {token}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeTargetHostCampaignError(
            "target-host campaign is not valid UTF-8 JSON"
        ) from error
    if type(value) is not dict:
        raise RuntimeTargetHostCampaignError(
            "target-host campaign must be a JSON object"
        )
    return value


def _string_tuple(value: object, *, name: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise RuntimeTargetHostCampaignError(f"{name} must be a JSON array")
    result = tuple(value)
    if any(type(item) is not str or not item for item in result):
        raise RuntimeTargetHostCampaignError(
            f"{name} must contain non-empty strings"
        )
    return result


def _integer_tuple(value: object, *, name: str) -> tuple[int, ...]:
    if type(value) is not list:
        raise RuntimeTargetHostCampaignError(f"{name} must be a JSON array")
    result = tuple(value)
    if any(type(item) is not int for item in result):
        raise RuntimeTargetHostCampaignError(
            f"{name} must contain exact integers"
        )
    return result


@dataclass(frozen=True, slots=True)
class ParsedRuntimeTargetHostCampaign:
    evidence: RuntimeLoadCampaignEvidence

    @property
    def collector_id(self) -> str:
        return COLLECTOR_ID

    @property
    def collector_version(self) -> str:
        return COLLECTOR_VERSION

    @property
    def canonical_bytes(self) -> bytes:
        return serialize_runtime_load_campaign_evidence(self.evidence)

    @property
    def payload_sha256(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def parse(cls, raw: bytes) -> "ParsedRuntimeTargetHostCampaign":
        value = _parse_json(raw)
        expected_top = {
            "schema_version",
            "evidence_type",
            "evidence_digest",
            "observation",
            "journal_sequence_before",
            "journal_sequence_after",
            "recovered_event_ids",
            "recovered_journal_sequences",
            "host_identity",
        }
        if set(value) != expected_top:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign fields are non-canonical"
            )
        if value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeTargetHostCampaignError(
                "unsupported target-host campaign schema_version"
            )
        if value.get("evidence_type") != EVIDENCE_TYPE:
            raise RuntimeTargetHostCampaignError(
                "unsupported target-host campaign evidence_type"
            )

        observation_value = value.get("observation")
        expected_observation = {
            "scenario_id",
            "spec_digest",
            "release_sha",
            "configuration_hash",
            "host_fingerprint",
            "expected_financial_events",
            "recovered_financial_events",
            "financial_latency_us",
            "financial_staleness_us",
            "research_interference_us",
            "reconnect_backlog_remaining",
            "declared_duration_us",
            "observed_duration_us",
        }
        if type(observation_value) is not dict or set(observation_value) != expected_observation:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign observation fields are non-canonical"
            )
        try:
            observation = RuntimeLoadObservation.create(
                scenario_id=observation_value["scenario_id"],
                spec_digest=observation_value["spec_digest"],
                release_sha=observation_value["release_sha"],
                configuration_hash=observation_value["configuration_hash"],
                host_fingerprint=observation_value["host_fingerprint"],
                expected_financial_events=observation_value["expected_financial_events"],
                recovered_financial_events=observation_value["recovered_financial_events"],
                financial_latency_us=observation_value["financial_latency_us"],
                financial_staleness_us=observation_value["financial_staleness_us"],
                research_interference_us=observation_value["research_interference_us"],
                reconnect_backlog_remaining=observation_value["reconnect_backlog_remaining"],
                declared_duration_us=observation_value["declared_duration_us"],
                observed_duration_us=observation_value["observed_duration_us"],
            )
        except (RuntimeBudgetError, TypeError, ValueError) as error:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign observation is invalid"
            ) from error

        recovered_ids = _string_tuple(
            value.get("recovered_event_ids"),
            name="recovered_event_ids",
        )
        recovered_sequences = _integer_tuple(
            value.get("recovered_journal_sequences"),
            name="recovered_journal_sequences",
        )
        before = value.get("journal_sequence_before")
        after = value.get("journal_sequence_after")
        if type(before) is not int or before < 0:
            raise RuntimeTargetHostCampaignError(
                "journal_sequence_before must be a non-negative integer"
            )
        if type(after) is not int or after < before:
            raise RuntimeTargetHostCampaignError(
                "journal_sequence_after must not precede the starting cut"
            )
        if len(recovered_ids) != observation.recovered_financial_events:
            raise RuntimeTargetHostCampaignError(
                "recovered campaign identities do not match observation count"
            )
        if len(recovered_ids) != len(set(recovered_ids)):
            raise RuntimeTargetHostCampaignError(
                "recovered campaign event identities must be unique"
            )
        if (
            len(recovered_sequences) != len(recovered_ids)
            or len(recovered_sequences) != len(set(recovered_sequences))
            or tuple(sorted(recovered_sequences)) != recovered_sequences
            or any(sequence <= before or sequence > after for sequence in recovered_sequences)
        ):
            raise RuntimeTargetHostCampaignError(
                "recovered campaign journal sequence cut is invalid"
            )

        host_identity = value.get("host_identity")
        if type(host_identity) is not dict:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign host_identity must be an object"
            )
        try:
            observed_host_fingerprint = host_identity_fingerprint(host_identity)
        except RuntimeTargetHostInventoryError as error:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign host identity is not canonical"
            ) from error
        if observed_host_fingerprint != observation.host_fingerprint:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign host identity conflicts with observation"
            )

        try:
            evidence = RuntimeLoadCampaignEvidence(
                observation=observation,
                journal_sequence_before=before,
                journal_sequence_after=after,
                recovered_event_ids=recovered_ids,
                recovered_journal_sequences=recovered_sequences,
                host_identity=host_identity,
            )
        except (TypeError, ValueError) as error:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign retained evidence is invalid"
            ) from error

        canonical_document = runtime_load_campaign_evidence_document(evidence)
        if canonical_document != value:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign digest or document semantics conflict"
            )
        parsed = cls(evidence=evidence)
        if parsed.canonical_bytes != raw:
            raise RuntimeTargetHostCampaignError(
                "target-host campaign bytes are not canonical retained evidence"
            )
        return parsed
