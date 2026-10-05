"""Fail-closed terminal economics assessment for deterministic baselines.

Public ``StrategyEconomicsBinding`` values are research evidence, not terminal
financial authority.  This composition can verify structural joins, effective
instrument-version shape, deterministic registered-run provenance, and optional
provider-cut replay consistency.  It intentionally cannot issue terminal
QUALIFIED economics until independently selected economic owners are composed.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, replace
from hashlib import sha256
import json
from threading import Lock
import weakref

from mvp.autotrade_mvp.instruments import InstrumentRegistry
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    ProviderEconomicCut,
    reverify_provider_economic_cut,
)
from research.autotrade_research.strategies.deterministic import (
    DeterministicProposal,
    RegisteredStrategyRunReceipt,
    StrategyEconomicsBinding,
    bind_strategy_economics,
    verify_registered_strategy_run,
)


class StrategyEconomicsAuthorityError(ValueError):
    """Terminal strategy-economics authority is unavailable or inconsistent."""


_BIND_STRATEGY_ECONOMICS = bind_strategy_economics
_VERIFY_REGISTERED_STRATEGY_RUN = verify_registered_strategy_run
_INSTRUMENT_REGISTRY_EXACT = InstrumentRegistry.exact
_INSTRUMENT_REGISTRY_AT = InstrumentRegistry.at
_REVERIFY_PROVIDER_ECONOMIC_CUT = reverify_provider_economic_cut

# A retained Python function still resolves its module globals at call time.
# Snapshot the replay primitives installed with the verifier so a later module
# rebind cannot silently redirect registered-run provenance while leaving the
# retained function object itself unchanged.
_REGISTERED_RUN_REPLAY_GLOBALS = _VERIFY_REGISTERED_STRATEGY_RUN.__globals__
_REGISTERED_RUN_REPLAY_DEPENDENCIES = tuple(
    (name, _REGISTERED_RUN_REPLAY_GLOBALS.get(name))
    for name in (
        "run_baseline",
        "_restore_threshold_strategy_snapshot",
        "_readmit_deterministic_proposal",
        "_readmit_registered_strategy_run_receipt",
    )
)

_BASE_REQUIRED_OWNERS = (
    "registered_strategy_run_receipt",
    "execution_calibration_authority",
    "capacity_evidence_authority",
    "after_cost_projection_authority",
    "instrument_registry_authority",
    "provider_economic_cut",
    "provider_scope_binding",
)
_ASSET_REQUIRED_OWNERS = {
    "PERPETUAL": ("funding_evidence_authority",),
    "OPTION": ("option_payoff_authority",),
    "FUTURE": ("futures_economics_authority",),
}


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise StrategyEconomicsAuthorityError(f"{name} must be exact non-empty text")
    return value


def _owner_name(value: object) -> str:
    text = _exact_text(value, name="economics owner").lower()
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in text):
        raise StrategyEconomicsAuthorityError(
            "economics owner names must use lowercase ASCII token syntax"
        )
    return text


def _snapshot_binding(value: object) -> StrategyEconomicsBinding:
    if type(value) is not StrategyEconomicsBinding:
        raise TypeError("economics_binding must be exact StrategyEconomicsBinding")
    try:
        return replace(value)
    except (TypeError, ValueError) as error:
        raise StrategyEconomicsAuthorityError(
            "economics binding cannot be reconstructed canonically"
        ) from error


def _snapshot_proposal(value: object) -> DeterministicProposal:
    if type(value) is not DeterministicProposal:
        raise TypeError("proposal must be exact DeterministicProposal")
    try:
        return replace(value)
    except (TypeError, ValueError) as error:
        raise StrategyEconomicsAuthorityError(
            "deterministic proposal cannot be reconstructed canonically"
        ) from error


def _require_installed_registered_run_replay_dependencies() -> None:
    for name, installed in _REGISTERED_RUN_REPLAY_DEPENDENCIES:
        if installed is None or _REGISTERED_RUN_REPLAY_GLOBALS.get(name) is not installed:
            raise StrategyEconomicsAuthorityError(
                "registered strategy-run verifier dependency changed"
            )


def _utc_text(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _diagnostic_structural_join_fingerprint(
    proposal: DeterministicProposal,
    economics: StrategyEconomicsBinding,
) -> str:
    """Validate shape without constructing receipt-less exposure.

    The canonical research binder deliberately rejects exposure without a
    verified registered-run receipt.  Terminal diagnostics must preserve that
    invariant while still representing the missing receipt as an unresolved
    owner, so this fallback validates only immutable identity fields and hashes
    them.  It does not produce an EconomicsBoundProposal or trading authority.
    """

    if (
        proposal.information_cutoff is None
        or proposal.horizon_seconds is None
        or proposal.expiry is None
        or proposal.strategy_fingerprint is None
        or proposal.strategy_configuration_fingerprint is None
    ):
        raise StrategyEconomicsAuthorityError(
            "proposal lacks registered strategy/horizon metadata"
        )
    checks = (
        (economics.strategy_fingerprint, proposal.strategy_fingerprint,
         "economics strategy fingerprint does not match proposal"),
        (economics.strategy_configuration_fingerprint,
         proposal.strategy_configuration_fingerprint,
         "economics strategy configuration fingerprint does not match proposal"),
        (economics.information_cutoff, proposal.information_cutoff,
         "economics information_cutoff does not match proposal"),
        (economics.decision_time, proposal.decision_time,
         "economics decision_time does not match proposal"),
        (economics.horizon_seconds, proposal.horizon_seconds,
         "economics horizon does not match proposal"),
        (economics.expiry, proposal.expiry,
         "economics expiry does not match proposal"),
    )
    for left, right, message in checks:
        if left != right:
            raise StrategyEconomicsAuthorityError(message)
    payload = {
        "schema_version": "wp33-diagnostic-structural-join.v1",
        "strategy_fingerprint": proposal.strategy_fingerprint,
        "strategy_configuration_fingerprint": proposal.strategy_configuration_fingerprint,
        "strategy_version": proposal.strategy_version,
        "symbol": proposal.symbol,
        "action": proposal.action,
        "quantity": str(proposal.quantity),
        "decision_time": _utc_text(proposal.decision_time),
        "information_cutoff": _utc_text(proposal.information_cutoff),
        "horizon_seconds": proposal.horizon_seconds,
        "expiry": _utc_text(proposal.expiry),
        "evidence_event_ids": list(proposal.evidence_event_ids),
        "economics_binding_sha256": economics.fingerprint,
        "instrument_version": economics.instrument_version,
    }
    rendered = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(rendered).hexdigest()


_ISSUE_TOKEN = object()
_ISSUED_LOCK = Lock()
_ISSUED: dict[
    int,
    tuple[
        weakref.ReferenceType["StrategyEconomicsAuthorityAssessment"],
        tuple[object, ...],
    ],
] = {}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class StrategyEconomicsAuthorityAssessment:
    """Module-issued statement about one exact proposal/economics owner graph."""

    status: str
    binding_fingerprint: str
    bound_proposal_fingerprint: str
    instrument_version: str
    instrument_provider_id: str
    verified_owners: tuple[str, ...]
    unresolved_owners: tuple[str, ...]
    registered_run_receipt_digest: str | None = None
    provider_economic_cut_digest: str | None = None
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ISSUE_TOKEN:
            raise StrategyEconomicsAuthorityError(
                "strategy economics assessment must be issued canonically"
            )
        if self.status != "INCONCLUSIVE":
            raise StrategyEconomicsAuthorityError(
                "positive strategy economics issuance is unavailable on current main"
            )
        for name in (
            "binding_fingerprint", "bound_proposal_fingerprint",
            "instrument_version", "instrument_provider_id",
        ):
            _exact_text(getattr(self, name), name=name)
        verified = tuple(_owner_name(item) for item in self.verified_owners)
        unresolved = tuple(_owner_name(item) for item in self.unresolved_owners)
        if len(set(verified)) != len(verified):
            raise StrategyEconomicsAuthorityError("verified owner list contains duplicates")
        if len(set(unresolved)) != len(unresolved):
            raise StrategyEconomicsAuthorityError("unresolved owner list contains duplicates")
        if set(verified) & set(unresolved):
            raise StrategyEconomicsAuthorityError(
                "an economics owner cannot be both verified and unresolved"
            )
        object.__setattr__(self, "verified_owners", verified)
        object.__setattr__(self, "unresolved_owners", unresolved)
        for name in ("registered_run_receipt_digest", "provider_economic_cut_digest"):
            value = getattr(self, name)
            if value is not None:
                _exact_text(value, name=name)

    @property
    def digest(self) -> str:
        payload = {
            "schema_version": "wp33-strategy-economics-authority.v2",
            "status": self.status,
            "binding_fingerprint": self.binding_fingerprint,
            "bound_proposal_fingerprint": self.bound_proposal_fingerprint,
            "instrument_version": self.instrument_version,
            "instrument_provider_id": self.instrument_provider_id,
            "verified_owners": list(self.verified_owners),
            "unresolved_owners": list(self.unresolved_owners),
            "registered_run_receipt_digest": self.registered_run_receipt_digest,
            "provider_economic_cut_digest": self.provider_economic_cut_digest,
        }
        rendered = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + sha256(rendered).hexdigest()


def _issued_seal(value: StrategyEconomicsAuthorityAssessment) -> tuple[object, ...]:
    return (
        value.status, value.binding_fingerprint, value.bound_proposal_fingerprint,
        value.instrument_version, value.instrument_provider_id,
        value.verified_owners, value.unresolved_owners,
        value.registered_run_receipt_digest, value.provider_economic_cut_digest,
        value.digest,
    )


def _register_issued(
    value: StrategyEconomicsAuthorityAssessment,
    *,
    _token: object,
) -> StrategyEconomicsAuthorityAssessment:
    if _token is not _ISSUE_TOKEN:
        raise StrategyEconomicsAuthorityError(
            "strategy economics assessment registration is private"
        )
    identity = id(value)
    seal = _issued_seal(value)

    def cleanup(reference) -> None:
        with _ISSUED_LOCK:
            current = _ISSUED.get(identity)
            if current is not None and current[0] is reference:
                _ISSUED.pop(identity, None)

    reference = weakref.ref(value, cleanup)
    with _ISSUED_LOCK:
        _ISSUED[identity] = (reference, seal)
    return value


def require_strategy_economics_assessment(
    value: object,
) -> StrategyEconomicsAuthorityAssessment:
    if type(value) is not StrategyEconomicsAuthorityAssessment:
        raise TypeError("value must be exact StrategyEconomicsAuthorityAssessment")
    with _ISSUED_LOCK:
        row = _ISSUED.get(id(value))
        if row is None or row[0]() is not value or row[1] != _issued_seal(value):
            raise StrategyEconomicsAuthorityError(
                "strategy economics assessment is unissued or changed"
            )
    return value


def assess_strategy_economics_authority(
    proposal: DeterministicProposal,
    economics_binding: StrategyEconomicsBinding,
    *,
    instrument_registry: InstrumentRegistry,
    registered_run_receipt: RegisteredStrategyRunReceipt | None = None,
    provider_economic_book: DurableProviderEconomicBook | None = None,
    provider_economic_cut: ProviderEconomicCut | None = None,
    expected_visibility_journal_sequence: int | None = None,
    additional_required_owners: tuple[str, ...] = (),
) -> StrategyEconomicsAuthorityAssessment:
    """Reverify deterministic provenance/durable facts and preserve owner gaps."""

    proposal = _snapshot_proposal(proposal)
    economics_binding = _snapshot_binding(economics_binding)
    if type(instrument_registry) is not InstrumentRegistry:
        raise TypeError("instrument_registry must be exact InstrumentRegistry")

    verified = {"instrument_registry_shape", "structural_economics_binding"}
    unresolved = set(_BASE_REQUIRED_OWNERS)
    receipt_digest: str | None = None

    if registered_run_receipt is not None:
        if type(registered_run_receipt) is not RegisteredStrategyRunReceipt:
            raise TypeError(
                "registered_run_receipt must be exact RegisteredStrategyRunReceipt"
            )
        _require_installed_registered_run_replay_dependencies()
        try:
            receipt_digest = _VERIFY_REGISTERED_STRATEGY_RUN(
                proposal, registered_run_receipt,
            )
        except (TypeError, ValueError) as error:
            raise StrategyEconomicsAuthorityError(
                "registered strategy-run receipt failed deterministic replay"
            ) from error
        if registered_run_receipt.instrument_version != economics_binding.instrument_version:
            raise StrategyEconomicsAuthorityError(
                "registered strategy-run receipt instrument does not match economics"
            )
        if economics_binding.registered_run_receipt_sha256 != receipt_digest:
            raise StrategyEconomicsAuthorityError(
                "economics binding does not name the replayed registered strategy run"
            )
        verified.add("registered_strategy_run_receipt")
        unresolved.remove("registered_strategy_run_receipt")

    if registered_run_receipt is not None or proposal.action == "HOLD":
        try:
            bound = _BIND_STRATEGY_ECONOMICS(
                proposal,
                economics_binding,
                instrument_version=economics_binding.instrument_version,
                registered_run_receipt=registered_run_receipt,
            )
        except (TypeError, ValueError) as error:
            raise StrategyEconomicsAuthorityError(
                "strategy economics structural join failed"
            ) from error
        bound_fingerprint = bound.fingerprint
    else:
        bound_fingerprint = _diagnostic_structural_join_fingerprint(
            proposal, economics_binding,
        )

    instrument = _INSTRUMENT_REGISTRY_EXACT(
        instrument_registry, economics_binding.instrument_version,
    )
    effective_instrument = _INSTRUMENT_REGISTRY_AT(
        instrument_registry,
        instrument.instrument_id,
        economics_binding.information_cutoff,
    )
    if effective_instrument != instrument:
        raise StrategyEconomicsAuthorityError(
            "instrument_version is not the registry version effective at information_cutoff"
        )

    # Deliberately do not equate proposal.symbol with provider_symbol here.
    # That mapping belongs to unresolved provider_scope_binding authority.
    unresolved.update(_ASSET_REQUIRED_OWNERS.get(instrument.asset_class, ()))
    if type(additional_required_owners) is not tuple:
        raise TypeError("additional_required_owners must be a tuple")
    unresolved.update(_owner_name(item) for item in additional_required_owners)
    unresolved.update(
        "dimension_" + _owner_name(item)
        for item in economics_binding.required_evidence_dimensions
    )

    values = (
        provider_economic_book,
        provider_economic_cut,
        expected_visibility_journal_sequence,
    )
    provided = tuple(value is not None for value in values)
    cut_digest: str | None = None
    if any(provided):
        if not all(provided):
            raise StrategyEconomicsAuthorityError(
                "provider economic verification requires book, cut and visibility together"
            )
        if type(provider_economic_book) is not DurableProviderEconomicBook:
            raise TypeError(
                "provider_economic_book must be exact DurableProviderEconomicBook"
            )
        if type(provider_economic_cut) is not ProviderEconomicCut:
            raise TypeError("provider_economic_cut must be exact ProviderEconomicCut")
        if (
            type(expected_visibility_journal_sequence) is not int
            or expected_visibility_journal_sequence <= 0
        ):
            raise StrategyEconomicsAuthorityError(
                "expected_visibility_journal_sequence must be a positive integer"
            )
        verified_cut = _REVERIFY_PROVIDER_ECONOMIC_CUT(
            provider_economic_book,
            provider_economic_cut,
            expected_visibility_journal_sequence=expected_visibility_journal_sequence,
        )
        if verified_cut.provider_id != instrument.provider_id:
            raise StrategyEconomicsAuthorityError(
                "provider economic cut does not match instrument provider"
            )
        verified.add("provider_economic_cut_replay")
        cut_digest = verified_cut.cut_digest

    assessment = StrategyEconomicsAuthorityAssessment(
        status="INCONCLUSIVE",
        binding_fingerprint=economics_binding.fingerprint,
        bound_proposal_fingerprint=bound_fingerprint,
        instrument_version=economics_binding.instrument_version,
        instrument_provider_id=instrument.provider_id,
        verified_owners=tuple(sorted(verified)),
        unresolved_owners=tuple(sorted(unresolved)),
        registered_run_receipt_digest=receipt_digest,
        provider_economic_cut_digest=cut_digest,
        _token=_ISSUE_TOKEN,
    )
    return _register_issued(assessment, _token=_ISSUE_TOKEN)


def require_qualified_strategy_economics(
    value: object,
) -> StrategyEconomicsAuthorityAssessment:
    """Fail closed until a non-caller-forgeable positive issuer exists."""

    assessment = require_strategy_economics_assessment(value)
    missing = ", ".join(assessment.unresolved_owners)
    raise StrategyEconomicsAuthorityError(
        "positive strategy economics issuance is unavailable on current main; "
        "terminal strategy economics is INCONCLUSIVE"
        + (f"; unresolved owners: {missing}" if missing else "")
    )
