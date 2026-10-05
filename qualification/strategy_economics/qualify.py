"""Fail-closed terminal economics assessment for deterministic baselines.

StrategyEconomicsBinding is a public research value. Its structural
status=QUALIFIED proves deterministic shape and causal metadata, not that the
referenced costs, capacity, FX/borrow/funding evidence, or after-cost values
came from independent canonical owners.

This qualification composition creates a separate issued assessment. The
current WP-33 composition can replay one exact registered strategy-run receipt,
re-run the structural economics join, resolve an exact caller-selected product
instrument version at the proposal information cutoff, and optionally reverify
a durable provider-economic cut from a caller-selected sealed book.
Registered-run replay proves deterministic computation provenance only.
Structural replay of caller-selected external sources is not proof that product
composition selected those economic owner authorities. The assessment
intentionally remains INCONCLUSIVE until the remaining WP-33 owner graph is
independently available. No green software test from this module is
profitability or economic-edge evidence.
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


# Retain installed structural/replay primitives. Later public module/class
# rebinding must not redirect which implementation this composition uses.
_BIND_STRATEGY_ECONOMICS = bind_strategy_economics
_VERIFY_REGISTERED_STRATEGY_RUN = verify_registered_strategy_run
_INSTRUMENT_REGISTRY_EXACT = InstrumentRegistry.exact
_INSTRUMENT_REGISTRY_AT = InstrumentRegistry.at
_REVERIFY_PROVIDER_ECONOMIC_CUT = reverify_provider_economic_cut

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
        raise StrategyEconomicsAuthorityError(
            f"{name} must be exact non-empty text"
        )
    return value


def _owner_name(value: object) -> str:
    text = _exact_text(value, name="economics owner").lower()
    if any(
        character
        not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in text
    ):
        raise StrategyEconomicsAuthorityError(
            "economics owner names must use lowercase ASCII token syntax"
        )
    return text


def _snapshot_binding(value: object) -> StrategyEconomicsBinding:
    if type(value) is not StrategyEconomicsBinding:
        raise TypeError(
            "economics_binding must be exact StrategyEconomicsBinding"
        )
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
    registered_run_receipt_digest: str | None
    provider_economic_cut_digest: str | None
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
            "binding_fingerprint",
            "bound_proposal_fingerprint",
            "instrument_version",
            "instrument_provider_id",
        ):
            _exact_text(getattr(self, name), name=name)
        verified = tuple(_owner_name(item) for item in self.verified_owners)
        unresolved = tuple(
            _owner_name(item) for item in self.unresolved_owners
        )
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
        object.__setattr__(self, "verified_owners", verified)
        object.__setattr__(self, "unresolved_owners", unresolved)
        for name in (
            "registered_run_receipt_digest",
            "provider_economic_cut_digest",
        ):
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
            "registered_run_receipt_digest": (
                self.registered_run_receipt_digest
            ),
            "provider_economic_cut_digest": (
                self.provider_economic_cut_digest
            ),
        }
        rendered = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + sha256(rendered).hexdigest()


def _issued_seal(
    value: StrategyEconomicsAuthorityAssessment,
) -> tuple[object, ...]:
    return (
        value.status,
        value.binding_fingerprint,
        value.bound_proposal_fingerprint,
        value.instrument_version,
        value.instrument_provider_id,
        value.verified_owners,
        value.unresolved_owners,
        value.registered_run_receipt_digest,
        value.provider_economic_cut_digest,
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
    seal = _issued_seal(value)
    identity = id(value)

    def cleanup(
        reference: weakref.ReferenceType[
            StrategyEconomicsAuthorityAssessment
        ],
    ) -> None:
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
    """Require an unchanged assessment issued by this composition."""

    if type(value) is not StrategyEconomicsAuthorityAssessment:
        raise TypeError(
            "value must be exact StrategyEconomicsAuthorityAssessment"
        )
    with _ISSUED_LOCK:
        row = _ISSUED.get(id(value))
        if (
            row is None
            or row[0]() is not value
            or row[1] != _issued_seal(value)
        ):
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
    """Reverify deterministic provenance/durable facts and preserve owner gaps.

    No caller-supplied verifier/callback is accepted. When supplied, the exact
    registered-run receipt is replayed through the retained installed verifier;
    only an exact replay whose digest is named by the economics binding can
    satisfy ``registered_strategy_run_receipt``. A missing receipt remains an
    explicit unresolved owner on this diagnostic INCONCLUSIVE path.

    Provider economics replay can verify that a supplied exact cut is reproduced
    by a supplied sealed book at one visibility sequence. Because this API does
    not select/authenticate that book as the product's economic owner, replay is
    recorded separately and does not satisfy ``provider_economic_cut``.
    """

    proposal = _snapshot_proposal(proposal)
    economics_binding = _snapshot_binding(economics_binding)
    if type(instrument_registry) is not InstrumentRegistry:
        raise TypeError(
            "instrument_registry must be exact InstrumentRegistry"
        )

    verified = {
        "instrument_registry_shape",
        "structural_economics_binding",
    }
    unresolved = set(_BASE_REQUIRED_OWNERS)
    receipt_digest: str | None = None

    if registered_run_receipt is not None:
        if type(registered_run_receipt) is not RegisteredStrategyRunReceipt:
            raise TypeError(
                "registered_run_receipt must be exact RegisteredStrategyRunReceipt"
            )
        try:
            receipt_digest = _VERIFY_REGISTERED_STRATEGY_RUN(
                proposal,
                registered_run_receipt,
            )
        except (TypeError, ValueError) as error:
            raise StrategyEconomicsAuthorityError(
                "registered strategy-run receipt failed deterministic replay"
            ) from error
        if (
            registered_run_receipt.instrument_version
            != economics_binding.instrument_version
        ):
            raise StrategyEconomicsAuthorityError(
                "registered strategy-run receipt instrument does not match economics"
            )
        if economics_binding.registered_run_receipt_sha256 != receipt_digest:
            raise StrategyEconomicsAuthorityError(
                "economics binding does not name the replayed registered strategy run"
            )
        verified.add("registered_strategy_run_receipt")
        unresolved.remove("registered_strategy_run_receipt")

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

    instrument = _INSTRUMENT_REGISTRY_EXACT(
        instrument_registry,
        economics_binding.instrument_version,
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
    if proposal.symbol != instrument.provider_symbol:
        raise StrategyEconomicsAuthorityError(
            "proposal symbol does not match instrument provider_symbol"
        )

    unresolved.update(
        _ASSET_REQUIRED_OWNERS.get(instrument.asset_class, ())
    )

    if type(additional_required_owners) is not tuple:
        raise TypeError("additional_required_owners must be a tuple")
    unresolved.update(
        _owner_name(item) for item in additional_required_owners
    )
    unresolved.update(
        "dimension_" + _owner_name(item)
        for item in economics_binding.required_evidence_dimensions
    )

    provider_values = (
        provider_economic_book,
        provider_economic_cut,
        expected_visibility_journal_sequence,
    )
    provided = tuple(value is not None for value in provider_values)
    cut_digest: str | None = None
    if any(provided):
        if not all(provided):
            raise StrategyEconomicsAuthorityError(
                "provider economic verification requires book, cut and "
                "visibility together"
            )
        if type(provider_economic_book) is not DurableProviderEconomicBook:
            raise TypeError(
                "provider_economic_book must be exact "
                "DurableProviderEconomicBook"
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
                "expected_visibility_journal_sequence must be a positive integer"
            )
        verified_cut = _REVERIFY_PROVIDER_ECONOMIC_CUT(
            provider_economic_book,
            provider_economic_cut,
            expected_visibility_journal_sequence=(
                expected_visibility_journal_sequence
            ),
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
        bound_proposal_fingerprint=bound.fingerprint,
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
    """Fail closed until a non-caller-forgeable positive issuer exists.

    Current main deliberately has no terminal positive WP-33 issuer. The
    in-process issuance registry is useful for detecting accidental mutation of
    diagnostic INCONCLUSIVE assessments, but Python module-private objects are
    not a security boundary: a same-process caller can import private symbols,
    mutate an object and attempt to re-register a matching seal. Therefore no
    registry state can promote an assessment to terminal financial authority.
    """

    assessment = require_strategy_economics_assessment(value)
    missing = ", ".join(assessment.unresolved_owners)
    raise StrategyEconomicsAuthorityError(
        "positive strategy economics issuance is unavailable on current main; "
        "terminal strategy economics is INCONCLUSIVE"
        + (f"; unresolved owners: {missing}" if missing else "")
    )
