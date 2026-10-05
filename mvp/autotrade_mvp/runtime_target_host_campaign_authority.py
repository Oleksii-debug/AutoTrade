"""Pre-run current-stack authority binding for WP-65 target-host campaigns.

The current runtime-load stack already has two canonical authorities that the
historical target-host stack used to carry on ``RuntimeCampaignPlan``/``Cut``:

* :class:`DeclaredRuntimeEventPlan` binds the exact physical JournalStore
  generation before financial work is run; and
* :mod:`journal_taxonomy` owns the versioned financial/non-financial
  classification used by qualification.

This module joins those existing authorities with the exact source/spec/host and
externally selected delivered-artifact identity in one *durable pre-run* record.
It deliberately does not create a second load plan, measurement, evaluator,
signer, chronology source, provider authority, release authority, or trading
authority.  A later raw target-host measurement/runner can only consume this
record after proving its samples follow the durable declaration cut.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from uuid import UUID

from .journal_taxonomy import taxonomy_digest
from .performance_qualification import RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .runtime_load_plan import (
    DeclaredRuntimeEventPlan,
    load_declared_runtime_event_plan,
)


_SCHEMA_VERSION = "1.0.0"
_EVENT_TYPE = "RuntimeQualificationTargetHostAuthorityDeclared"
# Reuse the existing qualification-control aggregate family rather than adding a
# second taxonomy authority for a declaration that is itself part of the runtime
# qualification plan/control plane.
_AGGREGATE_TYPE = "runtime_qualification_plan"
_AUTHORITY_TOKEN = object()
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class RuntimeTargetHostCampaignAuthorityError(ValueError):
    """Raised when a target-host pre-run authority binding is not canonical."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostCampaignAuthorityError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _sha256(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise RuntimeTargetHostCampaignAuthorityError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


def _git_sha(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _GIT_SHA.fullmatch(value) is None:
        raise RuntimeTargetHostCampaignAuthorityError(
            f"{name} must be a lowercase 40- or 64-character Git SHA"
        )
    return value


def _uuid(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise RuntimeTargetHostCampaignAuthorityError(
            f"{name} must be a canonical UUID"
        ) from error
    if canonical != value:
        raise RuntimeTargetHostCampaignAuthorityError(
            f"{name} must be a canonical UUID"
        )
    return value


def _snapshot_spec(value: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
    """Detach caller-owned spec state before durable authority I/O."""

    if type(value) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return RuntimeBudgetSpec(
        scenario_id=value.scenario_id,
        release_sha=value.release_sha,
        configuration_hash=value.configuration_hash,
        host_fingerprint=value.host_fingerprint,
        strategy_horizon_us=value.strategy_horizon_us,
        max_p95_financial_latency_us=value.max_p95_financial_latency_us,
        max_financial_staleness_us=value.max_financial_staleness_us,
        max_research_interference_us=value.max_research_interference_us,
        min_financial_samples=value.min_financial_samples,
        min_research_samples=value.min_research_samples,
    )


def _authority_event_id(authority_id: str) -> str:
    digest = sha256(authority_id.encode("utf-8")).hexdigest()
    return f"runtime-target-host-authority-{digest}"


def _authority_aggregate_id(authority_id: str) -> str:
    """Keep authority aggregate identity disjoint from financial-plan IDs."""

    return _authority_event_id(authority_id)


def _authority_payload(
    *,
    authority_id: str,
    spec: RuntimeBudgetSpec,
    financial_plan: DeclaredRuntimeEventPlan,
    release_artifact_id: str,
    release_artifact_sha256: str,
    journal_taxonomy_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "authority_id": authority_id,
        "scenario_id": spec.scenario_id,
        "spec_digest": spec.digest,
        "source_sha": spec.release_sha,
        "configuration_hash": spec.configuration_hash,
        "host_fingerprint": spec.host_fingerprint,
        "release_artifact_id": release_artifact_id,
        "release_artifact_sha256": release_artifact_sha256,
        "financial_plan_id": financial_plan.plan_id,
        "financial_plan_digest": financial_plan.digest,
        "store_identity_digest": financial_plan.store_identity_digest,
        "journal_taxonomy_digest": journal_taxonomy_digest,
    }


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostCampaignAuthority:
    """JournalStore-issued pre-run binding consumed by target-host measurement."""

    authority_id: str
    event_id: str
    scenario_id: str
    spec_digest: str
    source_sha: str
    configuration_hash: str
    host_fingerprint: str
    release_artifact_id: str
    release_artifact_sha256: str
    financial_plan_id: str
    financial_plan_digest: str
    store_identity_digest: str
    journal_taxonomy_digest: str
    declared_journal_sequence: int
    payload_hash: str
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _AUTHORITY_TOKEN:
            raise RuntimeTargetHostCampaignAuthorityError(
                "target-host campaign authority must come from canonical JournalStore"
            )

    @property
    def digest(self) -> str:
        return self.payload_hash


def _decode_authority(
    *,
    event: dict[str, object],
    spec: RuntimeBudgetSpec,
    authority_id: str,
    financial_plan: DeclaredRuntimeEventPlan,
) -> RuntimeTargetHostCampaignAuthority:
    expected_event_id = _authority_event_id(authority_id)
    if event.get("event_id") != expected_event_id:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority event identity conflicts"
        )
    if event.get("event_type") != _EVENT_TYPE:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority event type conflicts"
        )
    if event.get("aggregate_type") != _AGGREGATE_TYPE:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority aggregate type conflicts"
        )
    if (
        event.get("aggregate_id") != _authority_aggregate_id(authority_id)
        or event.get("aggregate_version") != 1
    ):
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority durable identity conflicts"
        )

    payload = event.get("payload")
    expected_keys = {
        "schema_version",
        "authority_id",
        "scenario_id",
        "spec_digest",
        "source_sha",
        "configuration_hash",
        "host_fingerprint",
        "release_artifact_id",
        "release_artifact_sha256",
        "financial_plan_id",
        "financial_plan_digest",
        "store_identity_digest",
        "journal_taxonomy_digest",
    }
    if type(payload) is not dict or set(payload) != expected_keys:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority payload shape conflicts"
        )
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority schema is unsupported"
        )
    if payload.get("authority_id") != authority_id:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority payload identity conflicts"
        )

    expected_taxonomy = _sha256(
        taxonomy_digest(),
        name="current journal taxonomy digest",
    )
    checks = {
        "scenario_id": (payload.get("scenario_id"), spec.scenario_id),
        "spec_digest": (payload.get("spec_digest"), spec.digest),
        "source_sha": (payload.get("source_sha"), spec.release_sha),
        "configuration_hash": (
            payload.get("configuration_hash"),
            spec.configuration_hash,
        ),
        "host_fingerprint": (payload.get("host_fingerprint"), spec.host_fingerprint),
        "financial_plan_id": (payload.get("financial_plan_id"), financial_plan.plan_id),
        "financial_plan_digest": (
            payload.get("financial_plan_digest"),
            financial_plan.digest,
        ),
        "store_identity_digest": (
            payload.get("store_identity_digest"),
            financial_plan.store_identity_digest,
        ),
        "journal_taxonomy_digest": (
            payload.get("journal_taxonomy_digest"),
            expected_taxonomy,
        ),
    }
    mismatches = [name for name, pair in checks.items() if pair[0] != pair[1]]
    if mismatches:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority no longer matches canonical campaign authority: "
            + ", ".join(mismatches)
        )

    release_artifact_id = _uuid(
        payload.get("release_artifact_id"),
        name="release_artifact_id",
    )
    release_artifact_sha256 = _sha256(
        payload.get("release_artifact_sha256"),
        name="release_artifact_sha256",
    )
    _git_sha(payload.get("source_sha"), name="source_sha")
    _sha256(payload.get("spec_digest"), name="spec_digest")
    _sha256(payload.get("configuration_hash"), name="configuration_hash")
    _sha256(payload.get("host_fingerprint"), name="host_fingerprint")
    _sha256(payload.get("financial_plan_digest"), name="financial_plan_digest")
    _sha256(payload.get("store_identity_digest"), name="store_identity_digest")

    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence <= financial_plan.declared_journal_sequence:
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority must be durably declared after its financial plan"
        )
    payload_hash = event.get("payload_hash")
    if type(payload_hash) is not str or payload_hash != payload_digest(payload):
        raise RuntimeTargetHostCampaignAuthorityError(
            "target-host authority payload hash conflicts"
        )

    return RuntimeTargetHostCampaignAuthority(
        authority_id=authority_id,
        event_id=expected_event_id,
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        source_sha=spec.release_sha,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
        financial_plan_id=financial_plan.plan_id,
        financial_plan_digest=financial_plan.digest,
        store_identity_digest=financial_plan.store_identity_digest,
        journal_taxonomy_digest=expected_taxonomy,
        declared_journal_sequence=sequence,
        payload_hash=payload_hash,
        _token=_AUTHORITY_TOKEN,
    )


def load_runtime_target_host_campaign_authority(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    authority_id: str,
) -> RuntimeTargetHostCampaignAuthority:
    """Reload and revalidate one exact pre-run target-host authority binding."""

    spec = _snapshot_spec(spec)
    authority_id = _text(authority_id, name="authority_id")
    store_identity = require_exact_journal_store_authority(
        store,
        subject="target-host qualification JournalStore",
    )
    with journal_store_authority_scope(store, store_identity):
        event = JournalStore.get_event(store, _authority_event_id(authority_id))
        if event is None:
            raise RuntimeTargetHostCampaignAuthorityError(
                "target-host campaign authority is not durably declared"
            )
        payload = event.get("payload")
        if type(payload) is not dict:
            raise RuntimeTargetHostCampaignAuthorityError(
                "target-host authority payload is invalid"
            )
        financial_plan_id = _text(
            payload.get("financial_plan_id"),
            name="financial_plan_id",
        )
        financial_plan = load_declared_runtime_event_plan(
            store,
            plan_id=financial_plan_id,
            spec=spec,
        )
        return _decode_authority(
            event=event,
            spec=spec,
            authority_id=authority_id,
            financial_plan=financial_plan,
        )


def declare_runtime_target_host_campaign_authority(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    authority_id: str,
    financial_plan_id: str,
    release_artifact_id: str,
    release_artifact_sha256: str,
) -> RuntimeTargetHostCampaignAuthority:
    """Durably bind current plan/store/taxonomy/release authority before a campaign.

    Exact redeclaration is idempotent. Reusing an authority identity with a
    different plan, artifact or current taxonomy fails closed. The returned value
    is still only a pre-run binding; it does not claim that target-host samples
    have been collected or that any performance/release gate passed.
    """

    spec = _snapshot_spec(spec)
    authority_id = _text(authority_id, name="authority_id")
    financial_plan_id = _text(financial_plan_id, name="financial_plan_id")
    release_artifact_id = _uuid(
        release_artifact_id,
        name="release_artifact_id",
    )
    release_artifact_sha256 = _sha256(
        release_artifact_sha256,
        name="release_artifact_sha256",
    )
    _git_sha(spec.release_sha, name="source_sha")

    store_identity = require_exact_journal_store_authority(
        store,
        subject="target-host qualification JournalStore",
    )
    with journal_store_authority_scope(store, store_identity):
        financial_plan = load_declared_runtime_event_plan(
            store,
            plan_id=financial_plan_id,
            spec=spec,
        )
        current_taxonomy = _sha256(
            taxonomy_digest(),
            name="current journal taxonomy digest",
        )
        payload = _authority_payload(
            authority_id=authority_id,
            spec=spec,
            financial_plan=financial_plan,
            release_artifact_id=release_artifact_id,
            release_artifact_sha256=release_artifact_sha256,
            journal_taxonomy_digest=current_taxonomy,
        )
        event_id = _authority_event_id(authority_id)
        existing = JournalStore.get_event(store, event_id)
        if existing is not None:
            loaded = _decode_authority(
                event=existing,
                spec=spec,
                authority_id=authority_id,
                financial_plan=financial_plan,
            )
            if (
                loaded.release_artifact_id != release_artifact_id
                or loaded.release_artifact_sha256 != release_artifact_sha256
                or loaded.financial_plan_id != financial_plan.plan_id
                or loaded.financial_plan_digest != financial_plan.digest
                or loaded.journal_taxonomy_digest != current_taxonomy
            ):
                raise RuntimeTargetHostCampaignAuthorityError(
                    "target-host campaign authority identity was already used for different content"
                )
            return loaded

        try:
            JournalStore.append_event(
                store,
                {
                    "event_id": event_id,
                    "event_type": _EVENT_TYPE,
                    "aggregate_type": _AGGREGATE_TYPE,
                    "aggregate_id": _authority_aggregate_id(authority_id),
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
        except ValueError:
            # A concurrent identical declaration may have won. Re-read through
            # the same canonical store authority and compare requested content.
            loaded = load_runtime_target_host_campaign_authority(
                store,
                spec,
                authority_id=authority_id,
            )
            if (
                loaded.release_artifact_id != release_artifact_id
                or loaded.release_artifact_sha256 != release_artifact_sha256
                or loaded.financial_plan_id != financial_plan.plan_id
                or loaded.financial_plan_digest != financial_plan.digest
                or loaded.journal_taxonomy_digest != current_taxonomy
            ):
                raise RuntimeTargetHostCampaignAuthorityError(
                    "target-host campaign authority identity was concurrently reused"
                )
            return loaded

        event = JournalStore.get_event(store, event_id)
        if event is None:
            raise RuntimeTargetHostCampaignAuthorityError(
                "target-host campaign authority disappeared after durable declaration"
            )
        return _decode_authority(
            event=event,
            spec=spec,
            authority_id=authority_id,
            financial_plan=financial_plan,
        )
