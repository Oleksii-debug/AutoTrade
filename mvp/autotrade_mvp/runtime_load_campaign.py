"""Measured frozen-runtime load campaign for WP-65 qualification.

The collector deliberately exercises the network-free frozen vertical slice and
derives RuntimeLoadObservation from durable journal evidence plus monotonic
wall-clock measurements. It grants no trading authority and makes no claim
beyond the declared host/configuration/scenario.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import subprocess
from time import perf_counter_ns
from types import MappingProxyType
from typing import Mapping, Sequence

from .performance_qualification import RuntimeBudgetSpec, RuntimeLoadObservation
from .persistence import JournalStore
from .pipeline import run_vertical_slice


_EVIDENCE_SCHEMA_VERSION = "1.1.0"


def _json_native_identity(value: object) -> object:
    """Normalize only inert built-in JSON carriers before identity hashing."""

    if value is None or type(value) in (str, bool, int, float):
        return value
    if type(value) is dict:
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("identity mapping keys must be exact strings")
            normalized[key] = _json_native_identity(item)
        return normalized
    if type(value) in (list, tuple):
        return [_json_native_identity(item) for item in value]
    raise TypeError(
        f"identity contains unsupported non-JSON value: {type(value).__name__}"
    )


def _sha256_identity(value: object) -> str:
    encoded = json.dumps(
        _json_native_identity(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _observed_source_sha() -> str:
    """Resolve the exact Git checkout measured by this qualification run."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("cannot resolve measured Git source identity") from error
    observed = result.stdout.strip()
    if (
        len(observed) not in {40, 64}
        or observed != observed.lower()
        or any(ch not in "0123456789abcdef" for ch in observed)
    ):
        raise RuntimeError("measured Git source identity is not canonical")
    return observed


def _require_exact_source(expected_source_sha: str) -> str:
    if (
        type(expected_source_sha) is not str
        or len(expected_source_sha) not in {40, 64}
        or expected_source_sha != expected_source_sha.strip().lower()
        or any(ch not in "0123456789abcdef" for ch in expected_source_sha)
    ):
        raise ValueError("runtime budget release_sha must be a canonical Git object id")
    observed = _observed_source_sha()
    if observed != expected_source_sha:
        raise RuntimeError(
            "runtime load qualification release_sha does not match actual Git checkout"
        )
    return observed


def capture_runtime_host_identity() -> dict[str, object]:
    """Capture stable-enough host/runtime facts as a detached inert built-in dict."""

    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def _recovered_simulation_event_records(
    store: JournalStore, *, after_sequence: int
) -> tuple[dict[str, object], ...]:
    """Return only canonical measured-slice events after an exact journal cut."""

    events = store.load_events_after_journal_sequence(after_sequence, limit=100000)
    selected = tuple(
        event
        for event in events
        if event.get("event_type") == "SimulationEpisodeRecorded"
        and event.get("aggregate_type") == "simulation_portfolio"
        and event.get("environment") == "SIMULATION"
    )
    ids = [event.get("event_id") for event in selected]
    sequences = [event.get("journal_sequence") for event in selected]
    if any(not isinstance(value, str) or not value for value in ids):
        raise ValueError("measured runtime event lacks durable event identity")
    if len(ids) != len(set(ids)):
        raise ValueError("measured runtime contains duplicate durable event identity")
    if any(type(value) is not int or value <= after_sequence for value in sequences):
        raise ValueError("measured runtime event lacks valid journal sequence")
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise ValueError("measured runtime journal ordering is invalid")
    return selected


@dataclass(frozen=True)
class RuntimeLoadCampaignEvidence:
    """Retained raw identity cut supporting one RuntimeLoadObservation."""

    observation: RuntimeLoadObservation
    journal_sequence_before: int
    journal_sequence_after: int
    recovered_event_ids: tuple[str, ...]
    recovered_journal_sequences: tuple[int, ...]
    host_identity: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.observation) is not RuntimeLoadObservation:
            raise TypeError("observation must be exact RuntimeLoadObservation")
        if type(self.journal_sequence_before) is not int or self.journal_sequence_before < 0:
            raise ValueError("journal_sequence_before must be non-negative integer")
        if (
            type(self.journal_sequence_after) is not int
            or self.journal_sequence_after < self.journal_sequence_before
        ):
            raise ValueError("journal_sequence_after must not precede the starting cut")
        if type(self.recovered_event_ids) is not tuple or any(
            type(event_id) is not str or not event_id
            for event_id in self.recovered_event_ids
        ):
            raise ValueError("recovered event identities must be exact non-empty strings")
        if type(self.recovered_journal_sequences) is not tuple or any(
            type(sequence) is not int
            or sequence <= self.journal_sequence_before
            or sequence > self.journal_sequence_after
            for sequence in self.recovered_journal_sequences
        ):
            raise ValueError("recovered journal sequences must lie inside the exact campaign cut")
        if len(self.recovered_event_ids) != len(set(self.recovered_event_ids)):
            raise ValueError("recovered event identities must be unique")
        if len(self.recovered_event_ids) != len(self.recovered_journal_sequences):
            raise ValueError("recovered event identities and sequences must align")
        if self.recovered_journal_sequences != tuple(sorted(self.recovered_journal_sequences)):
            raise ValueError("recovered journal sequences must be ordered")
        if len(self.recovered_journal_sequences) != len(set(self.recovered_journal_sequences)):
            raise ValueError("recovered journal sequences must be unique")
        if self.observation.recovered_financial_event_ids != self.recovered_event_ids:
            raise ValueError(
                "observation recovered financial identities must match retained journal cut"
            )
        normalized_host = _json_native_identity(self.host_identity)
        if type(normalized_host) is not dict:
            raise TypeError("host_identity must be an exact JSON object")
        object.__setattr__(self, "host_identity", MappingProxyType(normalized_host))

    @property
    def evidence_digest(self) -> str:
        return _sha256_identity(
            {
                "schema_version": _EVIDENCE_SCHEMA_VERSION,
                "scenario_id": self.observation.scenario_id,
                "spec_digest": self.observation.spec_digest,
                "release_sha": self.observation.release_sha,
                "configuration_hash": self.observation.configuration_hash,
                "host_fingerprint": self.observation.host_fingerprint,
                "journal_sequence_before": self.journal_sequence_before,
                "journal_sequence_after": self.journal_sequence_after,
                "recovered_event_ids": list(self.recovered_event_ids),
                "recovered_journal_sequences": list(self.recovered_journal_sequences),
                "expected_financial_events": self.observation.expected_financial_events,
                "recovered_financial_events": self.observation.recovered_financial_events,
                "financial_latency_us": list(self.observation.financial_latency_us),
                "financial_staleness_us": list(self.observation.financial_staleness_us),
                "research_interference_us": list(self.observation.research_interference_us),
                "recovered_financial_event_ids": list(
                    self.observation.recovered_financial_event_ids
                ),
                "financial_latency_event_ids": list(
                    self.observation.financial_latency_event_ids
                ),
                "financial_staleness_event_ids": list(
                    self.observation.financial_staleness_event_ids
                ),
                "reconnect_backlog_remaining": self.observation.reconnect_backlog_remaining,
                "declared_duration_us": self.observation.declared_duration_us,
                "observed_duration_us": self.observation.observed_duration_us,
                "host_identity": dict(self.host_identity),
            }
        )


def collect_vertical_slice_load_evidence(
    spec: RuntimeBudgetSpec,
    *,
    state_dir: str | Path,
    episodes: Sequence[Sequence[str | int]],
    configuration: object,
    host_identity: object,
    declared_duration_us: int,
) -> RuntimeLoadCampaignEvidence:
    """Run a declared simulation load and retain durable identity evidence.

    Each episode is timed independently. Expected count comes from the declared
    episode set; recovered count comes only from integrity-checked canonical
    SimulationEpisodeRecorded events after the exact pre-campaign journal cut.
    Unrelated durable events cannot compensate for a missing measured event.
    This collector intentionally leaves staleness and research-interference
    sample sets empty because this synchronous slice does not measure either
    dimension. It reports the actual durable outbox backlog rather than a
    synthetic zero, so the canonical evaluator cannot produce a favorable
    reconnect result from missing measurement authority.
    """

    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    _require_exact_source(spec.release_sha)
    if type(declared_duration_us) is not int or declared_duration_us <= 0:
        raise ValueError("declared_duration_us must be a positive integer")
    if isinstance(episodes, (str, bytes)) or not isinstance(episodes, Sequence) or not episodes:
        raise ValueError("episodes must be a non-empty sequence")
    if _sha256_identity(configuration) != spec.configuration_hash:
        raise ValueError("configuration does not match runtime budget spec")
    if _sha256_identity(host_identity) != spec.host_fingerprint:
        raise ValueError("host identity does not match runtime budget spec")

    root = Path(state_dir)
    journal_path = root / "journal.sqlite3"
    before = JournalStore(journal_path).current_journal_sequence() if journal_path.exists() else 0
    latencies: list[int] = []
    campaign_start = perf_counter_ns()
    for episode in episodes:
        started = perf_counter_ns()
        run_vertical_slice(episode, root)
        completed = perf_counter_ns()
        elapsed_us = max(0, (completed - started) // 1000)
        latencies.append(elapsed_us)
    campaign_us = max(1, (perf_counter_ns() - campaign_start) // 1000)
    store = JournalStore(journal_path)
    recovered_events = _recovered_simulation_event_records(store, after_sequence=before)
    recovered_ids = tuple(str(event["event_id"]) for event in recovered_events)
    after = store.current_journal_sequence()
    reconnect_backlog = store.pending_outbox_count()
    if type(reconnect_backlog) is not int or reconnect_backlog < 0:
        raise RuntimeError("canonical JournalStore returned invalid reconnect backlog")

    latency_event_ids = recovered_ids if len(latencies) == len(recovered_ids) else ()
    observation = RuntimeLoadObservation.create(
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        release_sha=spec.release_sha,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        expected_financial_events=len(episodes),
        recovered_financial_events=len(recovered_events),
        financial_latency_us=latencies,
        # This bounded collector does not have a wall-clock source-availability
        # signal or a concurrent research workload. Empty samples deliberately
        # prevent a terminal PASS rather than fabricating zero staleness or
        # interference evidence.
        financial_staleness_us=(),
        research_interference_us=(),
        reconnect_backlog_remaining=reconnect_backlog,
        declared_duration_us=declared_duration_us,
        observed_duration_us=campaign_us,
        recovered_financial_event_ids=recovered_ids,
        financial_latency_event_ids=latency_event_ids,
        financial_staleness_event_ids=(),
    )
    return RuntimeLoadCampaignEvidence(
        observation=observation,
        journal_sequence_before=before,
        journal_sequence_after=after,
        recovered_event_ids=recovered_ids,
        recovered_journal_sequences=tuple(
            int(event["journal_sequence"]) for event in recovered_events
        ),
        host_identity=(
            dict(host_identity)
            if isinstance(host_identity, Mapping)
            else {"identity": host_identity}
        ),
    )


def collect_vertical_slice_load_observation(
    spec: RuntimeBudgetSpec,
    *,
    state_dir: str | Path,
    episodes: Sequence[Sequence[str | int]],
    configuration: object,
    host_identity: object,
    declared_duration_us: int,
) -> RuntimeLoadObservation:
    """Compatibility facade returning the canonical evaluator observation."""

    return collect_vertical_slice_load_evidence(
        spec,
        state_dir=state_dir,
        episodes=episodes,
        configuration=configuration,
        host_identity=host_identity,
        declared_duration_us=declared_duration_us,
    ).observation


def runtime_load_campaign_evidence_document(
    evidence: RuntimeLoadCampaignEvidence,
) -> dict[str, object]:
    """Return the exact canonical retained-evidence document."""

    if not isinstance(evidence, RuntimeLoadCampaignEvidence):
        raise TypeError("evidence must be RuntimeLoadCampaignEvidence")
    observation = evidence.observation
    return {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "evidence_type": "AUTOTRADE_RUNTIME_LOAD_CAMPAIGN",
        "evidence_digest": evidence.evidence_digest,
        "observation": {
            "scenario_id": observation.scenario_id,
            "spec_digest": observation.spec_digest,
            "release_sha": observation.release_sha,
            "configuration_hash": observation.configuration_hash,
            "host_fingerprint": observation.host_fingerprint,
            "expected_financial_events": observation.expected_financial_events,
            "recovered_financial_events": observation.recovered_financial_events,
            "financial_latency_us": list(observation.financial_latency_us),
            "financial_staleness_us": list(observation.financial_staleness_us),
            "research_interference_us": list(observation.research_interference_us),
            "recovered_financial_event_ids": list(
                observation.recovered_financial_event_ids
            ),
            "financial_latency_event_ids": list(
                observation.financial_latency_event_ids
            ),
            "financial_staleness_event_ids": list(
                observation.financial_staleness_event_ids
            ),
            "reconnect_backlog_remaining": observation.reconnect_backlog_remaining,
            "declared_duration_us": observation.declared_duration_us,
            "observed_duration_us": observation.observed_duration_us,
        },
        "journal_sequence_before": evidence.journal_sequence_before,
        "journal_sequence_after": evidence.journal_sequence_after,
        "recovered_event_ids": list(evidence.recovered_event_ids),
        "recovered_journal_sequences": list(evidence.recovered_journal_sequences),
        "host_identity": dict(evidence.host_identity),
    }


def serialize_runtime_load_campaign_evidence(
    evidence: RuntimeLoadCampaignEvidence,
) -> bytes:
    """Canonical UTF-8 bytes suitable for content-addressed qualification evidence."""

    return (
        json.dumps(
            runtime_load_campaign_evidence_document(evidence),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_runtime_load_campaign_evidence(
    evidence: RuntimeLoadCampaignEvidence,
    path: str | Path,
) -> str:
    """Atomically persist retained evidence and return its SHA-256 identity."""

    target = Path(path)
    if target.exists() and (target.is_dir() or target.is_symlink()):
        raise ValueError("runtime load evidence target must be a regular file path")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_runtime_load_campaign_evidence(evidence)
    temporary = target.with_name(target.name + ".tmp")
    if temporary.exists() and temporary.is_symlink():
        raise ValueError("runtime load evidence temporary path cannot be a symlink")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "sha256:" + sha256(payload).hexdigest()


def load_runtime_load_campaign_evidence(
    path: str | Path,
) -> RuntimeLoadCampaignEvidence:
    """Verify canonical retained evidence bytes and reconstruct the measured cut."""

    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("runtime load evidence must be a regular file")
    raw = target.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("runtime load evidence is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "evidence_type",
        "evidence_digest",
        "observation",
        "journal_sequence_before",
        "journal_sequence_after",
        "recovered_event_ids",
        "recovered_journal_sequences",
        "host_identity",
    }:
        raise ValueError("runtime load evidence structure is not canonical")
    if (
        document.get("schema_version") != _EVIDENCE_SCHEMA_VERSION
        or document.get("evidence_type") != "AUTOTRADE_RUNTIME_LOAD_CAMPAIGN"
    ):
        raise ValueError("unsupported runtime load evidence schema")
    observation_raw = document.get("observation")
    if not isinstance(observation_raw, dict) or set(observation_raw) != {
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
        "recovered_financial_event_ids",
        "financial_latency_event_ids",
        "financial_staleness_event_ids",
        "reconnect_backlog_remaining",
        "declared_duration_us",
        "observed_duration_us",
    }:
        raise ValueError("runtime load observation structure is not canonical")
    host_identity = document.get("host_identity")
    if not isinstance(host_identity, dict):
        raise ValueError("runtime load host identity must be an object")
    recovered_ids = document.get("recovered_event_ids")
    recovered_sequences = document.get("recovered_journal_sequences")
    if not isinstance(recovered_ids, list) or not isinstance(recovered_sequences, list):
        raise ValueError("runtime load recovered event identity arrays are required")

    observation = RuntimeLoadObservation.create(
        scenario_id=observation_raw["scenario_id"],
        spec_digest=observation_raw["spec_digest"],
        release_sha=observation_raw["release_sha"],
        configuration_hash=observation_raw["configuration_hash"],
        host_fingerprint=observation_raw["host_fingerprint"],
        expected_financial_events=observation_raw["expected_financial_events"],
        recovered_financial_events=observation_raw["recovered_financial_events"],
        financial_latency_us=observation_raw["financial_latency_us"],
        financial_staleness_us=observation_raw["financial_staleness_us"],
        research_interference_us=observation_raw["research_interference_us"],
        recovered_financial_event_ids=observation_raw["recovered_financial_event_ids"],
        financial_latency_event_ids=observation_raw["financial_latency_event_ids"],
        financial_staleness_event_ids=observation_raw["financial_staleness_event_ids"],
        reconnect_backlog_remaining=observation_raw["reconnect_backlog_remaining"],
        declared_duration_us=observation_raw["declared_duration_us"],
        observed_duration_us=observation_raw["observed_duration_us"],
    )
    evidence = RuntimeLoadCampaignEvidence(
        observation=observation,
        journal_sequence_before=document["journal_sequence_before"],
        journal_sequence_after=document["journal_sequence_after"],
        recovered_event_ids=tuple(recovered_ids),
        recovered_journal_sequences=tuple(recovered_sequences),
        host_identity=host_identity,
    )
    if document.get("evidence_digest") != evidence.evidence_digest:
        raise ValueError("runtime load evidence digest does not match retained content")
    if raw != serialize_runtime_load_campaign_evidence(evidence):
        raise ValueError("runtime load evidence bytes are not canonical")
    return evidence
