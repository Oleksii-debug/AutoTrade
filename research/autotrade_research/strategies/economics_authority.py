"""Fail-closed terminal authority seam for deterministic strategy economics.

The public StrategyEconomicsBinding is intentionally a research DTO. A caller
can construct one with syntactically valid hashes, so it must never become a
terminal economic authority merely because its structural status says
QUALIFIED.

This module establishes the separate terminal boundary. Current main can
reverify some owner facts (notably the canonical instrument registry and a
durable provider-economic cut), but it does not yet expose the complete owner
graph required by WP-33: a canonical registered-run receipt, independently
rooted execution/calibration evidence, capacity evidence, and the full
after-cost projection owners. Therefore this seam is deliberately fail-closed:
it returns an issued INCONCLUSIVE assessment and refuses terminal projection
until those owners are composed here.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, replace
from threading import Lock
from typing import Mapping
import weakref

from mvp.autotrade_mvp.instruments import InstrumentRegistry
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    ProviderEconomicCut,
    reverify_provider_economic_cut,
)

from .deterministic import (
    DeterministicProposal,
    StrategyEconomicsBinding,
    bind_strategy_economics,
    to_decision_proposal,
)


class StrategyEconomicsAuthorityError(ValueError):
    """Raised when terminal economics authority is unavailable or inconsistent."""


_BASE_REQUIRED_OWNERS = (
    "registered_strategy_run_receipt",
    "execution_calibration_authority",
    "capacity_evidence_authority",
    "after_cost_projection_authority",
    "provider_economic_cut",
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
    if any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in text):
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


_ISSUE_TOKEN = object()
_ISSUED_LOCK = Lock()
_ISSUED: dict[
    int,
    tuple[weakref.ReferenceType["StrategyEconomicsAuthority"], tuple[object, ...]],
] = {}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class StrategyEconomicsAuthority:
    """Module-issued assessment for one exact proposal/economics join."""

    status: str
    binding_fingerprint: str
    bound_proposal_fingerprint: str
    instrument_version: str
    instrument_provider_id: str
    verified_owners: tuple[str, ...]
    unresolved_owners: tuple[str, ...]
    provider_economic_cut_digest: str | None
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ISSUE_TOKEN:
            raise StrategyEconomicsAuthorityError(
                "strategy economics authority must be issued by canonical assessment"
            )
        if self.status not in {"INCONCLUSIVE", "QUALIFIED"}:
            raise StrategyEconomicsAuthorityError(
                "strategy economics authority status is invalid"
            )
        for name in (
            "binding_fingerprint",
            "bound_proposal_fingerprint",
            "instrument_version",
            "instrument_provider_id",
        ):
            _exact_text(getattr(self, name), name=name)
        verified = tuple(_owner_name(item) for item in self.verified_owners)
        unresolved = tuple(_owner_name(item) for item in self.unresolved_owners)
        if len(set(verified)) != len(verified):
            raise StrategyEconomicsAuthorityError(
                "verified owner list contains duplicates"
            )
        if len(set(unresolved)) != len(unresolved):
            raise StrategyEconomicsAuthorityError(
                "unresolved owner list contains duplicates"
            )
        if set(verified) & set(unresolved):
            raise StrategyEconomicsAuthorityError(
                "an economics owner cannot be both verified and unresolved"
            )
        if self.status == "QUALIFIED" and unresolved:
            raise StrategyEconomicsAuthorityError(
                "QUALIFIED strategy economics cannot retain unresolved owners"
            )
        object.__setattr__(self, "verified_owners", verified)
        object.__setattr__(self, "unresolved_owners", unresolved)
        if self.provider_economic_cut_digest is not None:
            _exact_text(
                self.provider_economic_cut_digest,
                name="provider_economic_cut_digest",
            )


def _issued_seal(value: StrategyEconomicsAuthority) -> tuple[object, ...]:
    return (
        value.status,
        value.binding_fingerprint,
        value.bound_proposal_fingerprint,
        value.instrument_version,
        value.instrument_provider_id,
        value.verified_owners,
        value.unresolved_owners,
        value.provider_economic_cut_digest,
    )


def _register_issued(
    value: StrategyEconomicsAuthority,
    *,
    _token: object,
) -> StrategyEconomicsAuthority:
    if _token is not _ISSUE_TOKEN:
        raise StrategyEconomicsAuthorityError(
            "strategy economics authority registration is private"
        )
    seal = _issued_seal(value)
    identity = id(value)

    def cleanup(ref: weakref.ReferenceType[StrategyEconomicsAuthority]) -> None:
        with _ISSUED_LOCK:
            current = _ISSUED.get(identity)
            if current is not None and current[0] is ref:
                _ISSUED.pop(identity, None)

    ref = weakref.ref(value, cleanup)
    with _ISSUED_LOCK:
        _ISSUED[identity] = (ref, seal)
    return value


def require_strategy_economics_authority(
    value: object,
) -> StrategyEconomicsAuthority:
    """Require an unchanged assessment issued by this module."""

    if type(value) is not StrategyEconomicsAuthority:
        raise TypeError("value must be exact StrategyEconomicsAuthority")
    with _ISSUED_LOCK:
        row = _ISSUED.get(id(value))
        if row is None or row[0]() is not value or row[1] != _issued_seal(value):
            raise StrategyEconomicsAuthorityError(
                "strategy economics authority is unissued or changed after issuance"
            )
    return value


def assess_strategy_economics_authority(
    proposal: DeterministicProposal,
    economics_binding: StrategyEconomicsBinding,
    *,
    instrument_registry: InstrumentRegistry,
    provider_economic_book: DurableProviderEconomicBook | None = None,
    provider_economic_cut: ProviderEconomicCut | None = None,
    expected_visibility_journal_sequence: int | None = None,
    additional_required_owners: tuple[str, ...] = (),
) -> StrategyEconomicsAuthority:
    """Assess the currently verifiable owner graph without minting missing truth."""

    proposal = _snapshot_proposal(proposal)
    economics_binding = _snapshot_binding(economics_binding)
    if type(instrument_registry) is not InstrumentRegistry:
        raise TypeError("instrument_registry must be exact InstrumentRegistry")

    bound = bind_strategy_economics(
        proposal,
        economics_binding,
        instrument_version=economics_binding.instrument_version,
    )

    instrument = InstrumentRegistry.exact(
        instrument_registry,
        economics_binding.instrument_version,
    )
    verified = {"instrument_registry", "structural_economics_binding"}
    unresolved = set(_BASE_REQUIRED_OWNERS)
    unresolved.update(_ASSET_REQUIRED_OWNERS.get(instrument.asset_class, ()))

    if type(additional_required_owners) is not tuple:
        raise TypeError("additional_required_owners must be a tuple")
    unresolved.update(_owner_name(item) for item in additional_required_owners)
    unresolved.update(
        "dimension_" + _owner_name(item)
        for item in economics_binding.required_evidence_dimensions
    )

    provider_values = (
        provider_economic_book,
        provider_economic_cut,
        expected_visibility_journal_sequence,
    )
    supplied_provider_values = tuple(
        value is not None for value in provider_values
    )
    cut_digest: str | None = None
    if any(supplied_provider_values):
        if not all(supplied_provider_values):
            raise StrategyEconomicsAuthorityError(
                "provider economic verification requires book, cut and visibility together"
            )
        if type(provider_economic_book) is not DurableProviderEconomicBook:
            raise TypeError(
                "provider_economic_book must be exact DurableProviderEconomicBook"
            )
        if type(provider_economic_cut) is not ProviderEconomicCut:
            raise TypeError(
                "provider_economic_cut must be exact ProviderEconomicCut"
            )
        if (
            type(expected_visibility_journal_sequence) is not int
            or expected_visibility_journal_sequence <= 0
        ):
            raise StrategyEconomicsAuthorityError(
                "expected_visibility_journal_sequence must be positive integer"
            )
        verified_cut = reverify_provider_economic_cut(
            provider_economic_book,
            provider_economic_cut,
            expected_visibility_journal_sequence=(
                expected_visibility_journal_sequence
            ),
        )
        if verified_cut.provider_id != instrument.provider_id:
            raise StrategyEconomicsAuthorityError(
                "provider economic cut does not match instrument provider authority"
            )
        verified.add("provider_economic_cut")
        unresolved.discard("provider_economic_cut")
        cut_digest = verified_cut.cut_digest

    authority = StrategyEconomicsAuthority(
        status="INCONCLUSIVE",
        binding_fingerprint=economics_binding.fingerprint,
        bound_proposal_fingerprint=bound.fingerprint,
        instrument_version=economics_binding.instrument_version,
        instrument_provider_id=instrument.provider_id,
        verified_owners=tuple(sorted(verified)),
        unresolved_owners=tuple(sorted(unresolved)),
        provider_economic_cut_digest=cut_digest,
        _token=_ISSUE_TOKEN,
    )
    return _register_issued(authority, _token=_ISSUE_TOKEN)


def terminal_decision_proposal(
    authority: StrategyEconomicsAuthority,
    proposal: DeterministicProposal,
    *,
    proposal_id: str,
    instrument_version: str,
    economics_binding: StrategyEconomicsBinding,
    exit_policy_ref: str,
    compute_cost_currency: str,
    counterarguments: tuple[str, ...] = (),
) -> Mapping[str, object]:
    """Project an exposure-bearing terminal decision only from qualified authority."""

    authority = require_strategy_economics_authority(authority)
    if authority.status != "QUALIFIED":
        missing = ", ".join(authority.unresolved_owners)
        raise StrategyEconomicsAuthorityError(
            "terminal strategy economics is INCONCLUSIVE; unresolved owners: "
            + missing
        )

    proposal = _snapshot_proposal(proposal)
    economics_binding = _snapshot_binding(economics_binding)
    if economics_binding.fingerprint != authority.binding_fingerprint:
        raise StrategyEconomicsAuthorityError(
            "terminal economics binding does not match issued authority"
        )
    if instrument_version != authority.instrument_version:
        raise StrategyEconomicsAuthorityError(
            "terminal instrument version does not match issued authority"
        )
    bound = bind_strategy_economics(
        proposal,
        economics_binding,
        instrument_version=instrument_version,
    )
    if bound.fingerprint != authority.bound_proposal_fingerprint:
        raise StrategyEconomicsAuthorityError(
            "terminal proposal does not match issued economics authority"
        )
    return to_decision_proposal(
        proposal,
        proposal_id=proposal_id,
        instrument_version=instrument_version,
        economics_binding=economics_binding,
        exit_policy_ref=exit_policy_ref,
        compute_cost_currency=compute_cost_currency,
        counterarguments=counterarguments,
    )
