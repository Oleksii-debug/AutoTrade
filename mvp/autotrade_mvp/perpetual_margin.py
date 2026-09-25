"""Conservative perpetual margin and liquidation-headroom oracle.

This module is deliberately provider-neutral and non-authoritative. It does not
place orders, choose leverage, or post funding. It evaluates evidenced mark,
index, collateral conversion and provider margin tiers, then applies explicit
stress assumptions. Stale or incomplete evidence blocks new risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Literal, Sequence
from uuid import UUID

from research.autotrade_research.artifacts.store import ArtifactStore

from .capabilities import CapabilitySnapshot


class PerpetualMarginError(ValueError):
    pass


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PerpetualMarginError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise PerpetualMarginError(f"{name} must be a finite decimal")
    return result


def _non_negative(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0:
        raise PerpetualMarginError(f"{name} must be non-negative")
    return result


def _positive(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= 0:
        raise PerpetualMarginError(f"{name} must be positive")
    return result


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerpetualMarginError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise PerpetualMarginError(f"{name} must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise PerpetualMarginError(f"{name} must be an ISO-8601 instant") from error
    return parsed.astimezone(timezone.utc)


def _artifact_id(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        return str(UUID(text))
    except (ValueError, TypeError, AttributeError) as error:
        raise PerpetualMarginError(f"{name} must be an artifact UUID") from error


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _verify_immutable_artifact(
    store: ArtifactStore,
    *,
    artifact_id: str,
    expected_payload: object,
    expected_metadata: dict[str, object],
) -> None:
    if not isinstance(store, ArtifactStore):
        raise PerpetualMarginError(
            "canonical ArtifactStore is required for immutable margin evidence"
        )
    try:
        manifest = store.load_manifest(artifact_id)
        payload = store.read_bytes(artifact_id)
    except Exception as error:
        raise PerpetualMarginError(
            "immutable margin evidence artifact is missing or corrupt"
        ) from error
    if type(manifest) is not dict or not isinstance(payload, bytes):
        raise PerpetualMarginError(
            "immutable margin evidence artifact has unsupported representation"
        )
    if manifest.get("artifact_id") != artifact_id:
        raise PerpetualMarginError("margin evidence artifact identity mismatch")
    actual_digest = "sha256:" + sha256(payload).hexdigest()
    if manifest.get("sha256") != actual_digest:
        raise PerpetualMarginError("margin evidence artifact digest mismatch")
    manifest_hash = manifest.get("manifest_hash")
    if (
        not isinstance(manifest_hash, str)
        or len(manifest_hash) != 71
        or not manifest_hash.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in manifest_hash[7:])
    ):
        raise PerpetualMarginError(
            "margin evidence artifact manifest integrity binding is required"
        )
    rights = manifest.get("rights")
    if not isinstance(rights, dict) or rights.get("storage") is not True:
        raise PerpetualMarginError(
            "margin evidence artifact must preserve storage provenance"
        )
    if manifest.get("media_type") != "application/json":
        raise PerpetualMarginError("margin evidence artifact media type mismatch")
    metadata = manifest.get("metadata")
    if type(metadata) is not dict or any(
        metadata.get(key) != value for key, value in expected_metadata.items()
    ):
        raise PerpetualMarginError("margin evidence artifact scope metadata mismatch")
    if payload != _canonical_json_bytes(expected_payload):
        raise PerpetualMarginError(
            "margin evidence artifact content does not match supplied economics"
        )


@dataclass(frozen=True, slots=True)
class MarginTier:
    notional_upper_bound: Decimal
    maintenance_rate: Decimal
    maintenance_adjustment: Decimal = Decimal("0")
    adjustment_convention: Literal["ADD", "DEDUCT"] = "ADD"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "notional_upper_bound",
            _positive(self.notional_upper_bound, name="notional_upper_bound"),
        )
        rate = _non_negative(self.maintenance_rate, name="maintenance_rate")
        if rate > 1:
            raise PerpetualMarginError("maintenance_rate cannot exceed 1")
        object.__setattr__(self, "maintenance_rate", rate)
        object.__setattr__(
            self,
            "maintenance_adjustment",
            _non_negative(
                self.maintenance_adjustment,
                name="maintenance_adjustment",
            ),
        )
        if self.adjustment_convention not in {"ADD", "DEDUCT"}:
            raise PerpetualMarginError(
                "adjustment_convention must be ADD or DEDUCT"
            )

    def maintenance_requirement(self, notional: Decimal) -> Decimal:
        base = notional * self.maintenance_rate
        if self.adjustment_convention == "ADD":
            requirement = base + self.maintenance_adjustment
        else:
            requirement = base - self.maintenance_adjustment
        if requirement < 0:
            raise PerpetualMarginError(
                "margin tier formula produced negative maintenance"
            )
        return requirement


@dataclass(frozen=True, slots=True)
class PerpetualMarginEvidence:
    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    instrument_version: str
    capability_snapshot_id: str
    position_mode: str
    margin_mode: str
    collateral_currency: str
    settlement_currency: str
    risk_tier_revision: str
    evidence_bundle_ref: str
    tier_table_evidence_ref: str
    mark_price: Decimal
    index_price: Decimal
    collateral_fx_to_settlement: Decimal
    mark_observed_at: str
    index_observed_at: str
    collateral_fx_observed_at: str
    margin_tiers_observed_at: str
    margin_tiers: tuple[MarginTier, ...]

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "account_id",
            "entity_id",
            "instrument_version",
            "capability_snapshot_id",
            "position_mode",
            "collateral_currency",
            "settlement_currency",
            "risk_tier_revision",
        ):
            object.__setattr__(
                self,
                name,
                _text(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "evidence_bundle_ref",
            _artifact_id(self.evidence_bundle_ref, name="evidence_bundle_ref"),
        )
        object.__setattr__(
            self,
            "tier_table_evidence_ref",
            _artifact_id(
                self.tier_table_evidence_ref,
                name="tier_table_evidence_ref",
            ),
        )
        # Provider/account capability identity must use the exact canonical
        # representation owned by CapabilitySnapshot. Do not introduce a second
        # case-normalization rule inside margin evidence.
        object.__setattr__(
            self,
            "environment",
            _text(self.environment, name="environment").upper(),
        )
        object.__setattr__(
            self,
            "margin_mode",
            _text(self.margin_mode, name="margin_mode").upper(),
        )
        object.__setattr__(
            self,
            "collateral_currency",
            self.collateral_currency.upper(),
        )
        object.__setattr__(
            self,
            "settlement_currency",
            self.settlement_currency.upper(),
        )
        object.__setattr__(self, "mark_price", _positive(self.mark_price, name="mark_price"))
        object.__setattr__(self, "index_price", _positive(self.index_price, name="index_price"))
        object.__setattr__(
            self,
            "collateral_fx_to_settlement",
            _positive(
                self.collateral_fx_to_settlement,
                name="collateral_fx_to_settlement",
            ),
        )
        for name in (
            "mark_observed_at",
            "index_observed_at",
            "collateral_fx_observed_at",
            "margin_tiers_observed_at",
        ):
            _instant(getattr(self, name), name=name)
        tiers = tuple(self.margin_tiers)
        if not tiers:
            raise PerpetualMarginError("margin_tiers cannot be empty")
        prior = Decimal("0")
        for tier in tiers:
            if not isinstance(tier, MarginTier):
                raise TypeError("margin_tiers must contain MarginTier")
            if tier.notional_upper_bound <= prior:
                raise PerpetualMarginError(
                    "margin tier upper bounds must be strictly increasing"
                )
            prior = tier.notional_upper_bound
        object.__setattr__(self, "margin_tiers", tiers)

    @property
    def capability_identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.provider_id,
            self.account_id,
            self.entity_id,
            self.environment,
            self.instrument_version,
        )

    @property
    def tier_identity(self) -> tuple[str, str, str]:
        return (
            self.capability_snapshot_id,
            self.risk_tier_revision,
            self.tier_table_evidence_ref,
        )

    def tier_table_payload(self) -> dict[str, object]:
        return {
            "risk_tier_revision": self.risk_tier_revision,
            "tiers": [
                {
                    "notional_upper_bound": _decimal_text(
                        tier.notional_upper_bound
                    ),
                    "maintenance_rate": _decimal_text(tier.maintenance_rate),
                    "maintenance_adjustment": _decimal_text(
                        tier.maintenance_adjustment
                    ),
                    "adjustment_convention": tier.adjustment_convention,
                }
                for tier in self.margin_tiers
            ],
        }

    def evidence_bundle_payload(self) -> dict[str, object]:
        return {
            "mark_price": _decimal_text(self.mark_price),
            "index_price": _decimal_text(self.index_price),
            "collateral_fx_to_settlement": _decimal_text(
                self.collateral_fx_to_settlement
            ),
            "mark_observed_at": self.mark_observed_at,
            "index_observed_at": self.index_observed_at,
            "collateral_fx_observed_at": self.collateral_fx_observed_at,
            "margin_tiers_observed_at": self.margin_tiers_observed_at,
        }

    def verify_immutable_artifacts(self, store: ArtifactStore) -> None:
        common_metadata = {
            "schema_version": 1,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "entity_id": self.entity_id,
            "environment": self.environment,
            "instrument_version": self.instrument_version,
            "capability_snapshot_id": self.capability_snapshot_id,
            "position_mode": self.position_mode,
            "margin_mode": self.margin_mode,
            "collateral_currency": self.collateral_currency,
            "settlement_currency": self.settlement_currency,
            "risk_tier_revision": self.risk_tier_revision,
        }
        _verify_immutable_artifact(
            store,
            artifact_id=self.tier_table_evidence_ref,
            expected_payload=self.tier_table_payload(),
            expected_metadata={
                **common_metadata,
                "artifact_kind": "PERPETUAL_MARGIN_TIER_TABLE",
                "observed_at": self.margin_tiers_observed_at,
            },
        )
        _verify_immutable_artifact(
            store,
            artifact_id=self.evidence_bundle_ref,
            expected_payload=self.evidence_bundle_payload(),
            expected_metadata={
                **common_metadata,
                "artifact_kind": "PERPETUAL_MARGIN_EVIDENCE_BUNDLE",
                "observed_at": self.mark_observed_at,
            },
        )


@dataclass(frozen=True, slots=True)
class PerpetualStress:
    price_loss_fraction: Decimal
    collateral_fx_loss_fraction: Decimal
    exit_cost_fraction: Decimal
    additional_funding_loss: Decimal = Decimal("0")
    unavailable_exit_extra_loss: Decimal = Decimal("0")
    notional_increase_fraction: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in (
            "price_loss_fraction",
            "collateral_fx_loss_fraction",
            "exit_cost_fraction",
        ):
            value = _non_negative(getattr(self, name), name=name)
            if value > 1:
                raise PerpetualMarginError(f"{name} cannot exceed 1")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "additional_funding_loss",
            _non_negative(
                self.additional_funding_loss,
                name="additional_funding_loss",
            ),
        )
        object.__setattr__(
            self,
            "unavailable_exit_extra_loss",
            _non_negative(
                self.unavailable_exit_extra_loss,
                name="unavailable_exit_extra_loss",
            ),
        )
        notional_growth = _non_negative(
            self.notional_increase_fraction,
            name="notional_increase_fraction",
        )
        object.__setattr__(self, "notional_increase_fraction", notional_growth)


@dataclass(frozen=True, slots=True)
class PerpetualMarginResult:
    verdict: Literal["ALLOW_NEW_RISK", "BLOCK_NEW_RISK", "LIQUIDATION_STRESS"]
    notional: Decimal
    maintenance_requirement: Decimal
    stressed_maintenance_requirement: Decimal
    stressed_notional: Decimal
    current_equity_settlement: Decimal
    stressed_equity_settlement: Decimal
    liquidation_headroom: Decimal
    mark_index_divergence_bps: Decimal
    selected_tier_upper_bound: Decimal
    reasons: tuple[str, ...]


def _select_tier(notional: Decimal, tiers: Sequence[MarginTier]) -> MarginTier:
    for tier in tiers:
        if notional <= tier.notional_upper_bound:
            return tier
    raise PerpetualMarginError(
        "position notional exceeds evidenced margin tier coverage"
    )


def evaluate_perpetual_margin(
    *,
    capability: CapabilitySnapshot,
    instrument_version: str,
    margin_mode: str,
    collateral_currency: str,
    settlement_currency: str,
    risk_tier_revision: str,
    signed_notional_settlement,
    collateral_amount,
    unrealized_pnl_settlement,
    evidence: PerpetualMarginEvidence,
    artifact_store: ArtifactStore,
    stress: PerpetualStress,
    evaluated_at: str,
    maximum_evidence_age_seconds: int,
    maximum_mark_index_divergence_bps,
    collateral_haircut_fraction=0,
) -> PerpetualMarginResult:
    """Evaluate current/stressed margin with explicit freshness and depeg stress.

    signed_notional_settlement must already be calculated using the
    instrument's qualified linear/inverse payoff convention. This oracle never
    silently converts an inverse contract as though it were linear.
    """

    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if not isinstance(evidence, PerpetualMarginEvidence):
        raise TypeError("evidence must be PerpetualMarginEvidence")
    if not isinstance(stress, PerpetualStress):
        raise TypeError("stress must be PerpetualStress")
    if not isinstance(artifact_store, ArtifactStore):
        raise PerpetualMarginError(
            "canonical ArtifactStore is required for immutable margin evidence"
        )

    # Scope compatibility is an authority boundary and must be checked before
    # tier selection or any financial arithmetic.
    instrument = _text(instrument_version, name="instrument_version")
    requested_margin_mode = _text(margin_mode, name="margin_mode").upper()
    requested_collateral = _text(
        collateral_currency, name="collateral_currency"
    ).upper()
    requested_settlement = _text(
        settlement_currency, name="settlement_currency"
    ).upper()
    requested_revision = _text(
        risk_tier_revision, name="risk_tier_revision"
    )
    if instrument != evidence.instrument_version:
        raise PerpetualMarginError("instrument_version must match margin evidence")
    if capability.identity != evidence.capability_identity:
        raise PerpetualMarginError(
            "provider/account/entity/environment/instrument capability scope mismatch"
        )
    if capability.snapshot_id != evidence.capability_snapshot_id:
        raise PerpetualMarginError("capability snapshot does not match margin evidence")
    if capability.position_mode != evidence.position_mode:
        raise PerpetualMarginError("position mode does not match margin evidence")
    if requested_margin_mode != evidence.margin_mode:
        raise PerpetualMarginError("margin mode does not match margin evidence")
    if requested_collateral != evidence.collateral_currency:
        raise PerpetualMarginError("collateral currency does not match margin evidence")
    if requested_settlement != evidence.settlement_currency:
        raise PerpetualMarginError("settlement currency does not match margin evidence")
    if requested_revision != evidence.risk_tier_revision:
        raise PerpetualMarginError("risk tier revision does not match margin evidence")
    if capability.status != "VERIFIED":
        raise PerpetualMarginError("verified capability snapshot is required")

    evidence.verify_immutable_artifacts(artifact_store)

    now = _instant(evaluated_at, name="evaluated_at")
    if not (capability.observed_at <= now < capability.expires_at):
        raise PerpetualMarginError("capability snapshot is stale at evaluation time")
    if (
        isinstance(maximum_evidence_age_seconds, bool)
        or not isinstance(maximum_evidence_age_seconds, int)
        or maximum_evidence_age_seconds < 0
    ):
        raise PerpetualMarginError(
            "maximum_evidence_age_seconds must be a non-negative integer"
        )

    signed_notional = _decimal(
        signed_notional_settlement,
        name="signed_notional_settlement",
    )
    notional = abs(signed_notional)
    collateral = _non_negative(collateral_amount, name="collateral_amount")
    unrealized = _decimal(
        unrealized_pnl_settlement,
        name="unrealized_pnl_settlement",
    )
    haircut = _non_negative(
        collateral_haircut_fraction,
        name="collateral_haircut_fraction",
    )
    if haircut > 1:
        raise PerpetualMarginError("collateral_haircut_fraction cannot exceed 1")
    divergence_limit = _non_negative(
        maximum_mark_index_divergence_bps,
        name="maximum_mark_index_divergence_bps",
    )

    tier = _select_tier(notional, evidence.margin_tiers)
    maintenance = tier.maintenance_requirement(notional)
    stressed_notional = notional * (
        Decimal("1") + stress.notional_increase_fraction
    )
    stressed_tier = _select_tier(stressed_notional, evidence.margin_tiers)
    stressed_maintenance = stressed_tier.maintenance_requirement(stressed_notional)

    max_age = timedelta(seconds=maximum_evidence_age_seconds)
    reasons: list[str] = []
    for field in (
        "mark_observed_at",
        "index_observed_at",
        "collateral_fx_observed_at",
        "margin_tiers_observed_at",
    ):
        observed = _instant(getattr(evidence, field), name=field)
        if observed > now:
            reasons.append(f"{field}:FUTURE_EVIDENCE")
        elif now - observed > max_age:
            reasons.append(f"{field}:STALE")

    divergence = (
        abs(evidence.mark_price - evidence.index_price)
        / evidence.index_price
        * Decimal("10000")
    )
    if divergence > divergence_limit:
        reasons.append("MARK_INDEX_DIVERGENCE")

    current_collateral_value = (
        collateral
        * evidence.collateral_fx_to_settlement
        * (Decimal("1") - haircut)
    )
    current_equity = current_collateral_value + unrealized

    stressed_fx = evidence.collateral_fx_to_settlement * (
        Decimal("1") - stress.collateral_fx_loss_fraction
    )
    stressed_collateral_value = (
        collateral * stressed_fx * (Decimal("1") - haircut)
    )
    price_loss = notional * stress.price_loss_fraction
    exit_cost = notional * stress.exit_cost_fraction
    stressed_equity = (
        stressed_collateral_value
        + unrealized
        - price_loss
        - exit_cost
        - stress.additional_funding_loss
        - stress.unavailable_exit_extra_loss
    )
    headroom = stressed_equity - stressed_maintenance

    if current_equity < maintenance:
        reasons.append("CURRENT_MAINTENANCE_BREACH")
    if headroom < 0:
        reasons.append("STRESSED_LIQUIDATION_HEADROOM_NEGATIVE")

    if (
        "CURRENT_MAINTENANCE_BREACH" in reasons
        or "STRESSED_LIQUIDATION_HEADROOM_NEGATIVE" in reasons
    ):
        verdict = "LIQUIDATION_STRESS"
    elif reasons:
        verdict = "BLOCK_NEW_RISK"
    else:
        verdict = "ALLOW_NEW_RISK"

    return PerpetualMarginResult(
        verdict=verdict,
        notional=notional,
        maintenance_requirement=maintenance,
        stressed_maintenance_requirement=stressed_maintenance,
        stressed_notional=stressed_notional,
        current_equity_settlement=current_equity,
        stressed_equity_settlement=stressed_equity,
        liquidation_headroom=headroom,
        mark_index_divergence_bps=divergence,
        selected_tier_upper_bound=tier.notional_upper_bound,
        reasons=tuple(reasons),
    )
