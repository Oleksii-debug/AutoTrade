"""Independent deterministic risk admission for the AutoTrade foundation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from hashlib import sha256
import json
import re
from typing import Mapping, Sequence
from uuid import UUID


RISK_ACTIONS = frozenset({"TRADE", "REDUCE", "HEDGE", "FLATTEN", "EXERCISE"})
RISK_INSTRUMENT_TYPES = frozenset(
    {"GENERIC", "SPOT", "EQUITY", "FUTURE", "PERPETUAL", "OPTION"}
)


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value, *, name: str, allow_zero: bool = False) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


def _identity_key(value, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} keys must be non-empty strings")
    return value.strip()


def _normalize_mapping(values, *, name: str, parser) -> dict[str, Decimal]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, Decimal] = {}
    for raw_key, raw_value in values.items():
        key = _identity_key(raw_key, name=name)
        if key in normalized:
            raise ValueError(f"{name} keys must be unique after normalization")
        normalized[key] = parser(raw_value, key)
    return normalized


def _normalize_text_mapping(values, *, name: str) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = _identity_key(raw_key, name=name)
        if key in normalized:
            raise ValueError(f"{name} keys must be unique after normalization")
        normalized[key] = _identity_key(raw_value, name=f"{name} value")
    return normalized


def _normalize_actions(values, *, name: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of action names")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} values must be non-empty strings")
        action = value.strip().upper()
        if action not in RISK_ACTIONS:
            raise ValueError(f"Unsupported risk action: {action}")
        if action in normalized:
            raise ValueError(f"{name} values must be unique")
        normalized.append(action)
    if not normalized:
        raise ValueError(f"{name} must contain at least one action")
    return tuple(normalized)


def _normalize_labels(values, *, name: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of labels")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} values must be non-empty strings")
        label = value.strip()
        if label in normalized:
            raise ValueError(f"{name} values must be unique")
        normalized.append(label)
    return tuple(normalized)


def _normalize_nested_mapping(values, *, name: str) -> dict[str, dict[str, Decimal]]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, dict[str, Decimal]] = {}
    for raw_key, raw_value in values.items():
        key = _identity_key(raw_key, name=name)
        if key in normalized:
            raise ValueError(f"{name} keys must be unique after normalization")
        normalized[key] = _normalize_mapping(
            raw_value,
            name=f"{name}[{key}]",
            parser=lambda value, factor: _decimal(
                value,
                name=f"{name}[{key}][{factor}]",
            ),
        )
    return normalized


def _canonical_decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def stress_scenario_digest(scenario: Mapping[str, object]) -> str:
    """Content identity for one normalized deterministic stress scenario."""

    normalized = _normalize_mapping(
        scenario,
        name="stress_scenario",
        parser=lambda value, key: _decimal(
            value,
            name=f"stress shock {key}",
        ),
    )
    encoded = json.dumps(
        {
            key: _canonical_decimal_text(value)
            for key, value in sorted(normalized.items())
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def tail_scenario_set_digest(
    scenarios: Sequence[Mapping[str, object]],
) -> str:
    """Order-independent multiset identity for an equal-weight tail distribution."""

    if not isinstance(scenarios, Sequence) or isinstance(scenarios, (str, bytes)):
        raise TypeError("tail scenarios must be a sequence of mappings")
    scenario_digests = sorted(stress_scenario_digest(item) for item in scenarios)
    encoded = json.dumps(
        scenario_digests,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _normalize_scenario_digests(
    values: Mapping[str, str],
    *,
    name: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, str] = {}
    for raw_label, raw_digest in values.items():
        label = _identity_key(raw_label, name=name)
        if label in normalized:
            raise ValueError(f"{name} keys must be unique after normalization")
        if not isinstance(raw_digest, str):
            raise TypeError(f"{name}[{label}] must be a SHA-256 string")
        digest = raw_digest.strip()
        if (
            not digest.startswith("sha256:")
            or len(digest) != 71
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise ValueError(
                f"{name}[{label}] must be canonical lowercase sha256:<64-hex>"
            )
        normalized[label] = digest
    if not normalized:
        raise ValueError(f"{name} must contain at least one digest")
    return tuple(sorted(normalized.items()))



RISK_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _utc(value: datetime, *, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, name="timestamp").isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class LiquidationScope:
    provider_id: str
    account_id: str
    environment: str
    margin_mode: str
    risk_tier_version: str

    def __post_init__(self) -> None:
        provider = _identity_key(self.provider_id, name="provider_id").upper()
        account = _identity_key(self.account_id, name="account_id")
        environment = _identity_key(self.environment, name="environment").upper()
        if environment not in RISK_ENVIRONMENTS:
            raise ValueError("environment is unsupported")
        margin_mode = _identity_key(self.margin_mode, name="margin_mode").upper()
        tier = _identity_key(self.risk_tier_version, name="risk_tier_version")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "margin_mode", margin_mode)
        object.__setattr__(self, "risk_tier_version", tier)


@dataclass(frozen=True)
class LiquidationHeadroomEvidence:
    headroom: Decimal
    state_version: int
    provider_id: str
    account_id: str
    environment: str
    margin_mode: str
    risk_tier_version: str
    observed_at: datetime
    expires_at: datetime
    artifact_id: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.state_version, int)
            or isinstance(self.state_version, bool)
            or self.state_version < 0
        ):
            raise ValueError("liquidation evidence state_version must be non-negative")
        scope = LiquidationScope(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            margin_mode=self.margin_mode,
            risk_tier_version=self.risk_tier_version,
        )
        observed = _utc(self.observed_at, name="liquidation evidence observed_at")
        expires = _utc(self.expires_at, name="liquidation evidence expires_at")
        if expires <= observed:
            raise ValueError("liquidation evidence expires_at must follow observed_at")
        try:
            artifact_id = str(UUID(self.artifact_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("liquidation evidence artifact_id must be a UUID") from error
        digest = _identity_key(self.sha256, name="liquidation evidence sha256")
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(
                "liquidation evidence sha256 must be canonical lowercase sha256:<64-hex>"
            )
        object.__setattr__(
            self,
            "headroom",
            _decimal(self.headroom, name="liquidation evidence headroom"),
        )
        object.__setattr__(self, "provider_id", scope.provider_id)
        object.__setattr__(self, "account_id", scope.account_id)
        object.__setattr__(self, "environment", scope.environment)
        object.__setattr__(self, "margin_mode", scope.margin_mode)
        object.__setattr__(self, "risk_tier_version", scope.risk_tier_version)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "sha256", digest)

    @classmethod
    def create(cls, **values) -> "LiquidationHeadroomEvidence":
        return cls(**values)

    @property
    def scope(self) -> LiquidationScope:
        return LiquidationScope(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            margin_mode=self.margin_mode,
            risk_tier_version=self.risk_tier_version,
        )


def liquidation_evidence_payload(
    evidence: LiquidationHeadroomEvidence,
) -> dict[str, object]:
    if not isinstance(evidence, LiquidationHeadroomEvidence):
        raise TypeError("evidence must be LiquidationHeadroomEvidence")
    return {
        "artifact_kind": "LIQUIDATION_HEADROOM_EVIDENCE",
        "schema_version": 1,
        "provider_id": evidence.provider_id,
        "account_id": evidence.account_id,
        "environment": evidence.environment,
        "margin_mode": evidence.margin_mode,
        "risk_tier_version": evidence.risk_tier_version,
        "state_version": evidence.state_version,
        "observed_at": _utc_text(evidence.observed_at),
        "expires_at": _utc_text(evidence.expires_at),
        "headroom": _canonical_decimal_text(evidence.headroom),
    }


def _verify_liquidation_headroom_evidence(
    *,
    evidence: LiquidationHeadroomEvidence | None,
    expected_scope: LiquidationScope | None,
    expected_state_version: int,
    decision_time: datetime | None,
    evidence_store: object | None,
) -> bool:
    if (
        evidence is None
        or expected_scope is None
        or decision_time is None
        or evidence_store is None
    ):
        return False
    if evidence.scope != expected_scope or evidence.state_version != expected_state_version:
        return False
    point = _utc(decision_time, name="decision_time")
    if not (evidence.observed_at <= point < evidence.expires_at):
        return False
    try:
        manifest = evidence_store.load_manifest(evidence.artifact_id)
        raw = evidence_store.read_bytes(evidence.artifact_id)
    except Exception:
        return False
    if type(manifest) is not dict or not isinstance(raw, bytes):
        return False
    actual = "sha256:" + sha256(raw).hexdigest()
    if (
        manifest.get("artifact_id") != evidence.artifact_id
        or manifest.get("sha256") != evidence.sha256
        or actual != evidence.sha256
        or not manifest.get("manifest_hash")
    ):
        return False
    payload = liquidation_evidence_payload(evidence)
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if raw != canonical:
        return False
    metadata = manifest.get("metadata")
    if type(metadata) is not dict:
        return False
    return all(metadata.get(key) == value for key, value in payload.items())


@dataclass(frozen=True)
class RiskIntent:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    expected_state_version: int
    reduce_only: bool = False
    action: str = "TRADE"
    instrument_type: str = "GENERIC"

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        side: str,
        quantity,
        price,
        expected_state_version: int,
        reduce_only: bool = False,
        action: str = "TRADE",
        instrument_type: str = "GENERIC",
    ) -> "RiskIntent":
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_side = side.upper() if isinstance(side, str) else ""
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not isinstance(expected_state_version, int) or isinstance(expected_state_version, bool) or expected_state_version < 0:
            raise ValueError("expected_state_version must be a non-negative integer")
        if not isinstance(reduce_only, bool):
            raise TypeError("reduce_only must be a boolean")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("action is required")
        normalized_action = action.strip().upper()
        if normalized_action not in RISK_ACTIONS:
            raise ValueError(f"Unsupported risk action: {normalized_action}")
        if normalized_action in {"REDUCE", "FLATTEN"} and not reduce_only:
            raise ValueError(f"{normalized_action} action requires reduce_only")
        if not isinstance(instrument_type, str) or not instrument_type.strip():
            raise ValueError("instrument_type is required")
        normalized_instrument_type = instrument_type.strip().upper()
        if normalized_instrument_type not in RISK_INSTRUMENT_TYPES:
            raise ValueError(
                f"Unsupported risk instrument type: {normalized_instrument_type}"
            )
        if normalized_action == "EXERCISE" and normalized_instrument_type != "OPTION":
            raise ValueError("EXERCISE action requires OPTION instrument_type")
        return cls(
            symbol=symbol.strip(),
            side=normalized_side,
            quantity=_positive(quantity, name="quantity"),
            price=_positive(price, name="price"),
            expected_state_version=expected_state_version,
            reduce_only=reduce_only,
            action=normalized_action,
            instrument_type=normalized_instrument_type,
        )


@dataclass(frozen=True)
class RiskPolicy:
    max_abs_position: Decimal
    max_single_notional: Decimal
    max_gross_leverage: Decimal
    max_net_leverage: Decimal
    max_daily_loss: Decimal
    max_drawdown_fraction: Decimal
    max_data_age_seconds: Decimal
    max_fx_age_seconds: Decimal
    min_margin_headroom: Decimal
    max_stress_loss: Decimal
    max_expected_shortfall: Decimal | None = None
    expected_shortfall_tail_fraction: Decimal | None = None
    min_liquidation_headroom: Decimal | None = None
    required_stress_scenario_labels: tuple[str, ...] | None = None
    required_stress_scenario_digests: tuple[tuple[str, str], ...] | None = None
    required_tail_scenario_set_digest: str | None = None
    max_asset_concentration_fraction: Decimal | None = None
    max_venue_concentration_fraction: Decimal | None = None
    max_order_participation_fraction: Decimal | None = None
    max_abs_factor_exposure: Decimal | None = None
    max_spread_fraction: Decimal | None = None
    max_slippage_fraction: Decimal | None = None
    max_clock_age_seconds: Decimal | None = None
    allowed_actions: tuple[str, ...] | None = None
    require_settlement_evidence: bool = False
    require_option_exercise_evidence: bool = False
    min_futures_delivery_headroom_seconds: Decimal | None = None

    @classmethod
    def create(
        cls,
        *,
        max_abs_position,
        max_single_notional,
        max_gross_leverage,
        max_net_leverage,
        max_daily_loss,
        max_drawdown_fraction,
        max_data_age_seconds,
        max_fx_age_seconds,
        min_margin_headroom,
        max_stress_loss,
        max_expected_shortfall=None,
        expected_shortfall_tail_fraction=None,
        min_liquidation_headroom=None,
        required_stress_scenario_labels: Sequence[str] | None = None,
        required_stress_scenario_digests: Mapping[str, str] | None = None,
        required_tail_scenario_set_digest: str | None = None,
        max_asset_concentration_fraction=None,
        max_venue_concentration_fraction=None,
        max_order_participation_fraction=None,
        max_abs_factor_exposure=None,
        max_spread_fraction=None,
        max_slippage_fraction=None,
        max_clock_age_seconds=None,
        allowed_actions: Sequence[str] | None = None,
        require_settlement_evidence: bool = False,
        require_option_exercise_evidence: bool = False,
        min_futures_delivery_headroom_seconds=None,
    ) -> "RiskPolicy":
        values = {
            "max_abs_position": _positive(max_abs_position, name="max_abs_position"),
            "max_single_notional": _positive(max_single_notional, name="max_single_notional"),
            "max_gross_leverage": _positive(max_gross_leverage, name="max_gross_leverage"),
            "max_net_leverage": _positive(max_net_leverage, name="max_net_leverage"),
            "max_daily_loss": _positive(max_daily_loss, name="max_daily_loss", allow_zero=True),
            "max_drawdown_fraction": _positive(max_drawdown_fraction, name="max_drawdown_fraction", allow_zero=True),
            "max_data_age_seconds": _positive(max_data_age_seconds, name="max_data_age_seconds", allow_zero=True),
            "max_fx_age_seconds": _positive(max_fx_age_seconds, name="max_fx_age_seconds", allow_zero=True),
            "min_margin_headroom": _positive(min_margin_headroom, name="min_margin_headroom", allow_zero=True),
            "max_stress_loss": _positive(max_stress_loss, name="max_stress_loss", allow_zero=True),
        }
        if values["max_drawdown_fraction"] > 1:
            raise ValueError("max_drawdown_fraction cannot exceed 1")

        expected_shortfall_limit = (
            None
            if max_expected_shortfall is None
            else _positive(
                max_expected_shortfall,
                name="max_expected_shortfall",
                allow_zero=True,
            )
        )
        expected_shortfall_tail = (
            None
            if expected_shortfall_tail_fraction is None
            else _positive(
                expected_shortfall_tail_fraction,
                name="expected_shortfall_tail_fraction",
            )
        )
        if (expected_shortfall_limit is None) != (expected_shortfall_tail is None):
            raise ValueError(
                "max_expected_shortfall and expected_shortfall_tail_fraction "
                "must be configured together"
            )
        if expected_shortfall_tail is not None and expected_shortfall_tail > 1:
            raise ValueError("expected_shortfall_tail_fraction cannot exceed 1")
        liquidation_headroom_limit = (
            None
            if min_liquidation_headroom is None
            else _positive(
                min_liquidation_headroom,
                name="min_liquidation_headroom",
                allow_zero=True,
            )
        )
        normalized_required_stress_labels = (
            None
            if required_stress_scenario_labels is None
            else _normalize_labels(
                required_stress_scenario_labels,
                name="required_stress_scenario_labels",
            )
        )
        if normalized_required_stress_labels == ():
            raise ValueError(
                "required_stress_scenario_labels must contain at least one label"
            )
        normalized_required_stress_digests = (
            None
            if required_stress_scenario_digests is None
            else _normalize_scenario_digests(
                required_stress_scenario_digests,
                name="required_stress_scenario_digests",
            )
        )
        if (normalized_required_stress_labels is None) != (
            normalized_required_stress_digests is None
        ):
            raise ValueError(
                "required stress scenario labels and digests must be configured together"
            )
        if normalized_required_stress_labels is not None:
            digest_labels = {
                label for label, _digest in normalized_required_stress_digests or ()
            }
            if digest_labels != set(normalized_required_stress_labels):
                raise ValueError(
                    "required_stress_scenario_digests keys must exactly match required labels"
                )

        normalized_tail_set_digest = None
        if required_tail_scenario_set_digest is not None:
            if not isinstance(required_tail_scenario_set_digest, str):
                raise TypeError(
                    "required_tail_scenario_set_digest must be a SHA-256 string"
                )
            normalized_tail_set_digest = required_tail_scenario_set_digest.strip()
            if (
                not normalized_tail_set_digest.startswith("sha256:")
                or len(normalized_tail_set_digest) != 71
                or any(
                    ch not in "0123456789abcdef"
                    for ch in normalized_tail_set_digest[7:]
                )
            ):
                raise ValueError(
                    "required_tail_scenario_set_digest must be canonical "
                    "lowercase sha256:<64-hex>"
                )
        if expected_shortfall_limit is not None and normalized_tail_set_digest is None:
            raise ValueError(
                "expected-shortfall policy requires a frozen tail distribution digest"
            )
        if expected_shortfall_limit is None and normalized_tail_set_digest is not None:
            raise ValueError(
                "tail distribution digest requires expected-shortfall policy"
            )

        optional_limits: dict[str, Decimal | None] = {}
        for name, raw_value in (
            ("max_asset_concentration_fraction", max_asset_concentration_fraction),
            ("max_venue_concentration_fraction", max_venue_concentration_fraction),
            ("max_order_participation_fraction", max_order_participation_fraction),
            ("max_spread_fraction", max_spread_fraction),
            ("max_slippage_fraction", max_slippage_fraction),
        ):
            if raw_value is None:
                optional_limits[name] = None
                continue
            fraction = _positive(raw_value, name=name, allow_zero=True)
            if fraction > 1:
                raise ValueError(f"{name} cannot exceed 1")
            optional_limits[name] = fraction

        factor_limit = (
            None
            if max_abs_factor_exposure is None
            else _positive(
                max_abs_factor_exposure,
                name="max_abs_factor_exposure",
                allow_zero=True,
            )
        )
        clock_limit = (
            None
            if max_clock_age_seconds is None
            else _positive(
                max_clock_age_seconds,
                name="max_clock_age_seconds",
                allow_zero=True,
            )
        )
        normalized_allowed_actions = (
            None
            if allowed_actions is None
            else _normalize_actions(allowed_actions, name="allowed_actions")
        )
        if not isinstance(require_settlement_evidence, bool):
            raise TypeError("require_settlement_evidence must be a boolean")
        if not isinstance(require_option_exercise_evidence, bool):
            raise TypeError("require_option_exercise_evidence must be a boolean")
        delivery_headroom = (
            None
            if min_futures_delivery_headroom_seconds is None
            else _positive(
                min_futures_delivery_headroom_seconds,
                name="min_futures_delivery_headroom_seconds",
                allow_zero=True,
            )
        )
        return cls(
            **values,
            max_expected_shortfall=expected_shortfall_limit,
            expected_shortfall_tail_fraction=expected_shortfall_tail,
            min_liquidation_headroom=liquidation_headroom_limit,
            required_stress_scenario_labels=normalized_required_stress_labels,
            required_stress_scenario_digests=normalized_required_stress_digests,
            required_tail_scenario_set_digest=normalized_tail_set_digest,
            **optional_limits,
            max_abs_factor_exposure=factor_limit,
            max_clock_age_seconds=clock_limit,
            allowed_actions=normalized_allowed_actions,
            require_settlement_evidence=require_settlement_evidence,
            require_option_exercise_evidence=require_option_exercise_evidence,
            min_futures_delivery_headroom_seconds=delivery_headroom,
        )


@dataclass(frozen=True)
class RiskContext:
    state_version: int
    equity: Decimal
    positions: Mapping[str, Decimal]
    marks: Mapping[str, Decimal]
    reserved_position_delta: Mapping[str, Decimal]
    daily_pnl: Decimal
    drawdown_fraction: Decimal
    market_data_age_seconds: Decimal
    fx_age_seconds: Mapping[str, Decimal]
    fx_required: bool
    margin_headroom: Decimal
    capability_allowed: bool
    borrow_available: bool | None
    stress_scenarios: Sequence[Mapping[str, Decimal]]
    stress_scenario_labels: tuple[str, ...] = ()
    tail_scenarios: Sequence[Mapping[str, Decimal]] = ()
    liquidation_headroom: Decimal | None = None
    liquidation_scope: LiquidationScope | None = None
    liquidation_headroom_evidence: LiquidationHeadroomEvidence | None = None
    decision_time: datetime | None = None
    asset_buckets: Mapping[str, str] | None = None
    venues: Mapping[str, str] | None = None
    liquidity_capacity: Mapping[str, Decimal] | None = None
    factor_loadings: Mapping[str, Mapping[str, Decimal]] | None = None
    spread_fraction: Mapping[str, Decimal] | None = None
    slippage_fraction: Mapping[str, Decimal] | None = None
    clock_age_seconds: Decimal | None = None
    settlement_allowed: bool | None = None
    option_deliverable_verified: bool | None = None
    option_exercise_cash_required: Decimal | None = None
    option_exercise_cash_available: Decimal | None = None
    futures_delivery_headroom_seconds: Mapping[str, Decimal] | None = None

    @classmethod
    def create(
        cls,
        *,
        state_version: int,
        equity,
        positions: Mapping[str, object],
        marks: Mapping[str, object],
        reserved_position_delta: Mapping[str, object] | None = None,
        daily_pnl=0,
        drawdown_fraction=0,
        market_data_age_seconds=0,
        fx_age_seconds: Mapping[str, object] | None = None,
        fx_required: bool = False,
        margin_headroom,
        capability_allowed: bool,
        borrow_available: bool | None,
        stress_scenarios: Sequence[Mapping[str, object]] = (),
        stress_scenario_labels: Sequence[str] = (),
        tail_scenarios: Sequence[Mapping[str, object]] = (),
        liquidation_headroom=None,
        liquidation_scope: LiquidationScope | None = None,
        liquidation_headroom_evidence: LiquidationHeadroomEvidence | None = None,
        decision_time: datetime | None = None,
        asset_buckets: Mapping[str, str] | None = None,
        venues: Mapping[str, str] | None = None,
        liquidity_capacity: Mapping[str, object] | None = None,
        factor_loadings: Mapping[str, Mapping[str, object]] | None = None,
        spread_fraction: Mapping[str, object] | None = None,
        slippage_fraction: Mapping[str, object] | None = None,
        clock_age_seconds=None,
        settlement_allowed: bool | None = None,
        option_deliverable_verified: bool | None = None,
        option_exercise_cash_required=None,
        option_exercise_cash_available=None,
        futures_delivery_headroom_seconds: Mapping[str, object] | None = None,
    ) -> "RiskContext":
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        normalized_positions = _normalize_mapping(
            positions,
            name="positions",
            parser=lambda value, key: _decimal(value, name=f"position {key}"),
        )
        normalized_marks = _normalize_mapping(
            marks,
            name="marks",
            parser=lambda value, key: _positive(value, name=f"mark {key}"),
        )
        normalized_reserved = _normalize_mapping(
            reserved_position_delta or {},
            name="reserved_position_delta",
            parser=lambda value, key: _decimal(
                value,
                name=f"reserved position {key}",
            ),
        )
        normalized_fx = _normalize_mapping(
            fx_age_seconds or {},
            name="fx_age_seconds",
            parser=lambda value, key: _positive(
                value,
                name=f"FX age {key}",
                allow_zero=True,
            ),
        )
        normalized_asset_buckets = _normalize_text_mapping(
            asset_buckets or {},
            name="asset_buckets",
        )
        normalized_venues = _normalize_text_mapping(
            venues or {},
            name="venues",
        )
        normalized_liquidity = _normalize_mapping(
            liquidity_capacity or {},
            name="liquidity_capacity",
            parser=lambda value, key: _positive(
                value,
                name=f"liquidity capacity {key}",
                allow_zero=True,
            ),
        )
        normalized_factor_loadings = _normalize_nested_mapping(
            factor_loadings or {},
            name="factor_loadings",
        )
        normalized_spread = _normalize_mapping(
            spread_fraction or {},
            name="spread_fraction",
            parser=lambda value, key: _positive(
                value,
                name=f"spread fraction {key}",
                allow_zero=True,
            ),
        )
        normalized_slippage = _normalize_mapping(
            slippage_fraction or {},
            name="slippage_fraction",
            parser=lambda value, key: _positive(
                value,
                name=f"slippage fraction {key}",
                allow_zero=True,
            ),
        )
        normalized_clock_age = (
            None
            if clock_age_seconds is None
            else _positive(
                clock_age_seconds,
                name="clock_age_seconds",
                allow_zero=True,
            )
        )
        normalized_exercise_required = (
            None
            if option_exercise_cash_required is None
            else _positive(
                option_exercise_cash_required,
                name="option_exercise_cash_required",
                allow_zero=True,
            )
        )
        normalized_exercise_available = (
            None
            if option_exercise_cash_available is None
            else _positive(
                option_exercise_cash_available,
                name="option_exercise_cash_available",
                allow_zero=True,
            )
        )
        normalized_delivery_headroom = _normalize_mapping(
            futures_delivery_headroom_seconds or {},
            name="futures_delivery_headroom_seconds",
            parser=lambda value, key: _decimal(
                value,
                name=f"futures delivery headroom {key}",
            ),
        )
        if not isinstance(stress_scenarios, Sequence) or isinstance(
            stress_scenarios,
            (str, bytes),
        ):
            raise TypeError("stress_scenarios must be a sequence of mappings")
        scenarios = tuple(
            _normalize_mapping(
                scenario,
                name=f"stress_scenarios[{index}]",
                parser=lambda value, key: _decimal(
                    value,
                    name=f"stress shock {key}",
                ),
            )
            for index, scenario in enumerate(stress_scenarios)
        )
        normalized_stress_labels = _normalize_labels(
            stress_scenario_labels,
            name="stress_scenario_labels",
        )
        if normalized_stress_labels and len(normalized_stress_labels) != len(scenarios):
            raise ValueError(
                "stress_scenario_labels must align one-to-one with stress_scenarios"
            )
        if not isinstance(tail_scenarios, Sequence) or isinstance(
            tail_scenarios,
            (str, bytes),
        ):
            raise TypeError("tail_scenarios must be a sequence of mappings")
        normalized_tail_scenarios = tuple(
            _normalize_mapping(
                scenario,
                name=f"tail_scenarios[{index}]",
                parser=lambda value, key: _decimal(
                    value,
                    name=f"tail return {key}",
                ),
            )
            for index, scenario in enumerate(tail_scenarios)
        )
        normalized_liquidation_headroom = (
            None
            if liquidation_headroom is None
            else _decimal(
                liquidation_headroom,
                name="liquidation_headroom",
            )
        )

        if liquidation_scope is not None and not isinstance(
            liquidation_scope, LiquidationScope
        ):
            raise TypeError("liquidation_scope must be LiquidationScope or None")
        normalized_scope = (
            None
            if liquidation_scope is None
            else LiquidationScope(
                provider_id=liquidation_scope.provider_id,
                account_id=liquidation_scope.account_id,
                environment=liquidation_scope.environment,
                margin_mode=liquidation_scope.margin_mode,
                risk_tier_version=liquidation_scope.risk_tier_version,
            )
        )
        if liquidation_headroom_evidence is not None and not isinstance(
            liquidation_headroom_evidence, LiquidationHeadroomEvidence
        ):
            raise TypeError(
                "liquidation_headroom_evidence must be "
                "LiquidationHeadroomEvidence or None"
            )
        normalized_liquidation_evidence = (
            None
            if liquidation_headroom_evidence is None
            else LiquidationHeadroomEvidence.create(
                headroom=liquidation_headroom_evidence.headroom,
                state_version=liquidation_headroom_evidence.state_version,
                provider_id=liquidation_headroom_evidence.provider_id,
                account_id=liquidation_headroom_evidence.account_id,
                environment=liquidation_headroom_evidence.environment,
                margin_mode=liquidation_headroom_evidence.margin_mode,
                risk_tier_version=liquidation_headroom_evidence.risk_tier_version,
                observed_at=liquidation_headroom_evidence.observed_at,
                expires_at=liquidation_headroom_evidence.expires_at,
                artifact_id=liquidation_headroom_evidence.artifact_id,
                sha256=liquidation_headroom_evidence.sha256,
            )
        )
        normalized_decision_time = (
            None
            if decision_time is None
            else _utc(decision_time, name="decision_time")
        )
        if normalized_liquidation_evidence is not None:
            if normalized_scope is None or normalized_decision_time is None:
                raise ValueError(
                    "liquidation evidence requires exact scope and decision_time"
                )
            if normalized_liquidation_evidence.scope != normalized_scope:
                raise ValueError(
                    "liquidation evidence scope differs from risk context scope"
                )
            if normalized_liquidation_evidence.state_version != state_version:
                raise ValueError(
                    "liquidation evidence state_version differs from risk context"
                )
            if (
                normalized_liquidation_headroom is not None
                and normalized_liquidation_headroom
                != normalized_liquidation_evidence.headroom
            ):
                raise ValueError(
                    "liquidation_headroom differs from immutable evidence"
                )
            normalized_liquidation_headroom = normalized_liquidation_evidence.headroom
        normalized_drawdown = _positive(
            drawdown_fraction,
            name="drawdown_fraction",
            allow_zero=True,
        )
        if normalized_drawdown > 1:
            raise ValueError("drawdown_fraction cannot exceed 1")
        if not isinstance(fx_required, bool):
            raise TypeError("fx_required must be a boolean")
        if not isinstance(capability_allowed, bool):
            raise TypeError("capability_allowed must be a boolean")
        if borrow_available is not None and not isinstance(borrow_available, bool):
            raise TypeError("borrow_available must be a boolean or None")
        if settlement_allowed is not None and not isinstance(settlement_allowed, bool):
            raise TypeError("settlement_allowed must be a boolean or None")
        if (
            option_deliverable_verified is not None
            and not isinstance(option_deliverable_verified, bool)
        ):
            raise TypeError("option_deliverable_verified must be a boolean or None")
        return cls(
            state_version=state_version,
            equity=_positive(equity, name="equity"),
            positions=normalized_positions,
            marks=normalized_marks,
            reserved_position_delta=normalized_reserved,
            daily_pnl=_decimal(daily_pnl, name="daily_pnl"),
            drawdown_fraction=normalized_drawdown,
            market_data_age_seconds=_positive(market_data_age_seconds, name="market_data_age_seconds", allow_zero=True),
            fx_age_seconds=normalized_fx,
            fx_required=fx_required,
            margin_headroom=_positive(margin_headroom, name="margin_headroom", allow_zero=True),
            capability_allowed=capability_allowed,
            borrow_available=borrow_available,
            stress_scenarios=scenarios,
            stress_scenario_labels=normalized_stress_labels,
            tail_scenarios=normalized_tail_scenarios,
            liquidation_headroom=normalized_liquidation_headroom,
            liquidation_scope=normalized_scope,
            liquidation_headroom_evidence=normalized_liquidation_evidence,
            decision_time=normalized_decision_time,
            asset_buckets=normalized_asset_buckets,
            venues=normalized_venues,
            liquidity_capacity=normalized_liquidity,
            factor_loadings=normalized_factor_loadings,
            spread_fraction=normalized_spread,
            slippage_fraction=normalized_slippage,
            clock_age_seconds=normalized_clock_age,
            settlement_allowed=settlement_allowed,
            option_deliverable_verified=option_deliverable_verified,
            option_exercise_cash_required=normalized_exercise_required,
            option_exercise_cash_available=normalized_exercise_available,
            futures_delivery_headroom_seconds=normalized_delivery_headroom,
        )


@dataclass(frozen=True)
class RiskRuleResult:
    rule: str
    passed: bool
    observed: str
    limit: str
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    admitted: bool
    resulting_position: Decimal
    gross_leverage: Decimal
    net_leverage: Decimal
    worst_stress_loss: Decimal
    input_fingerprint: str
    rules: tuple[RiskRuleResult, ...]


def _fingerprint_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc_text(value)
    if hasattr(value, "__dataclass_fields__"):
        return _fingerprint_value(vars(value))
    if isinstance(value, Mapping):
        return {
            str(key): _fingerprint_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_fingerprint_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(
        f"Unsupported normalized risk fingerprint value: {type(value).__name__}"
    )


def _risk_input_fingerprint(
    intent: RiskIntent,
    context: RiskContext,
    policy: RiskPolicy,
) -> str:
    payload = {
        "intent": _fingerprint_value(vars(intent)),
        "context": _fingerprint_value(vars(context)),
        "policy": _fingerprint_value(vars(policy)),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def risk_decision_fingerprint(decision: RiskDecision) -> str:
    if not isinstance(decision, RiskDecision):
        raise TypeError("decision must be a RiskDecision")
    payload = {
        "admitted": decision.admitted,
        "resulting_position": str(decision.resulting_position),
        "gross_leverage": str(decision.gross_leverage),
        "net_leverage": str(decision.net_leverage),
        "worst_stress_loss": str(decision.worst_stress_loss),
        "input_fingerprint": decision.input_fingerprint,
        "rules": [
            {
                "rule": item.rule,
                "passed": item.passed,
                "observed": item.observed,
                "limit": item.limit,
                "reason": item.reason,
            }
            for item in decision.rules
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def evaluate_risk(
    intent: RiskIntent,
    context: RiskContext,
    policy: RiskPolicy,
    *,
    evidence_store: object | None = None,
) -> RiskDecision:
    if not isinstance(intent, RiskIntent):
        raise TypeError("intent must be RiskIntent")
    if not isinstance(context, RiskContext):
        raise TypeError("context must be RiskContext")
    if not isinstance(policy, RiskPolicy):
        raise TypeError("policy must be RiskPolicy")

    intent = RiskIntent.create(
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        price=intent.price,
        expected_state_version=intent.expected_state_version,
        reduce_only=intent.reduce_only,
        action=intent.action,
        instrument_type=intent.instrument_type,
    )
    context = RiskContext.create(
        state_version=context.state_version,
        equity=context.equity,
        positions=context.positions,
        marks=context.marks,
        reserved_position_delta=context.reserved_position_delta,
        daily_pnl=context.daily_pnl,
        drawdown_fraction=context.drawdown_fraction,
        market_data_age_seconds=context.market_data_age_seconds,
        fx_age_seconds=context.fx_age_seconds,
        fx_required=context.fx_required,
        margin_headroom=context.margin_headroom,
        capability_allowed=context.capability_allowed,
        borrow_available=context.borrow_available,
        stress_scenarios=context.stress_scenarios,
        stress_scenario_labels=context.stress_scenario_labels,
        tail_scenarios=context.tail_scenarios,
        liquidation_headroom=context.liquidation_headroom,
        liquidation_scope=context.liquidation_scope,
        liquidation_headroom_evidence=context.liquidation_headroom_evidence,
        decision_time=context.decision_time,
        asset_buckets=context.asset_buckets,
        venues=context.venues,
        liquidity_capacity=context.liquidity_capacity,
        factor_loadings=context.factor_loadings,
        spread_fraction=context.spread_fraction,
        slippage_fraction=context.slippage_fraction,
        clock_age_seconds=context.clock_age_seconds,
        settlement_allowed=context.settlement_allowed,
        option_deliverable_verified=context.option_deliverable_verified,
        option_exercise_cash_required=context.option_exercise_cash_required,
        option_exercise_cash_available=context.option_exercise_cash_available,
        futures_delivery_headroom_seconds=context.futures_delivery_headroom_seconds,
    )
    policy = RiskPolicy.create(
        max_abs_position=policy.max_abs_position,
        max_single_notional=policy.max_single_notional,
        max_gross_leverage=policy.max_gross_leverage,
        max_net_leverage=policy.max_net_leverage,
        max_daily_loss=policy.max_daily_loss,
        max_drawdown_fraction=policy.max_drawdown_fraction,
        max_data_age_seconds=policy.max_data_age_seconds,
        max_fx_age_seconds=policy.max_fx_age_seconds,
        min_margin_headroom=policy.min_margin_headroom,
        max_stress_loss=policy.max_stress_loss,
        max_expected_shortfall=policy.max_expected_shortfall,
        expected_shortfall_tail_fraction=policy.expected_shortfall_tail_fraction,
        min_liquidation_headroom=policy.min_liquidation_headroom,
        required_stress_scenario_labels=policy.required_stress_scenario_labels,
        required_stress_scenario_digests=(
            None
            if policy.required_stress_scenario_digests is None
            else dict(policy.required_stress_scenario_digests)
        ),
        required_tail_scenario_set_digest=policy.required_tail_scenario_set_digest,
        max_asset_concentration_fraction=policy.max_asset_concentration_fraction,
        max_venue_concentration_fraction=policy.max_venue_concentration_fraction,
        max_order_participation_fraction=policy.max_order_participation_fraction,
        max_abs_factor_exposure=policy.max_abs_factor_exposure,
        max_spread_fraction=policy.max_spread_fraction,
        max_slippage_fraction=policy.max_slippage_fraction,
        max_clock_age_seconds=policy.max_clock_age_seconds,
        allowed_actions=policy.allowed_actions,
        require_settlement_evidence=policy.require_settlement_evidence,
        require_option_exercise_evidence=policy.require_option_exercise_evidence,
        min_futures_delivery_headroom_seconds=policy.min_futures_delivery_headroom_seconds,
    )

    if intent.symbol not in context.marks:
        raise ValueError(f"Missing mark for {intent.symbol}")
    signed = intent.quantity if intent.side == "BUY" else -intent.quantity
    current = context.positions.get(intent.symbol, Decimal("0"))
    reserved = context.reserved_position_delta.get(intent.symbol, Decimal("0"))
    base_position = current + reserved
    resulting = base_position + signed

    base_positions = dict(context.positions)
    for symbol, delta in context.reserved_position_delta.items():
        base_positions[symbol] = base_positions.get(symbol, Decimal("0")) + delta
    projected_positions = dict(base_positions)
    projected_positions[intent.symbol] = projected_positions.get(intent.symbol, Decimal("0")) + signed

    missing_marks = [symbol for symbol, qty in projected_positions.items() if qty != 0 and symbol not in context.marks]
    if missing_marks:
        raise ValueError(f"Missing marks for positions: {', '.join(sorted(missing_marks))}")

    base_notionals = {
        symbol: qty * context.marks[symbol]
        for symbol, qty in base_positions.items()
        if qty != 0
    }
    notionals = {
        symbol: qty * context.marks[symbol]
        for symbol, qty in projected_positions.items()
        if qty != 0
    }
    base_gross = sum((abs(value) for value in base_notionals.values()), Decimal("0"))
    base_net = abs(sum(base_notionals.values(), Decimal("0")))
    gross = sum((abs(value) for value in notionals.values()), Decimal("0"))
    net = abs(sum(notionals.values(), Decimal("0")))
    gross_leverage = gross / context.equity
    net_leverage = net / context.equity
    mark_notional = abs(resulting * context.marks[intent.symbol])
    intent_notional = intent.quantity * intent.price
    single_notional = max(mark_notional, intent_notional)

    asset_concentration = Decimal("0")
    asset_concentration_complete = True
    missing_asset_buckets: set[str] = set()
    if policy.max_asset_concentration_fraction is not None and gross > 0:
        asset_groups: dict[str, Decimal] = {}
        asset_map = context.asset_buckets or {}
        for symbol, notional in notionals.items():
            bucket = asset_map.get(symbol)
            if bucket is None:
                missing_asset_buckets.add(symbol)
                continue
            asset_groups[bucket] = asset_groups.get(bucket, Decimal("0")) + abs(notional)
        asset_concentration_complete = not missing_asset_buckets
        if asset_concentration_complete and asset_groups:
            asset_concentration = max(asset_groups.values()) / gross

    venue_concentration = Decimal("0")
    venue_concentration_complete = True
    missing_venues: set[str] = set()
    if policy.max_venue_concentration_fraction is not None and gross > 0:
        venue_groups: dict[str, Decimal] = {}
        venue_map = context.venues or {}
        for symbol, notional in notionals.items():
            venue = venue_map.get(symbol)
            if venue is None:
                missing_venues.add(symbol)
                continue
            venue_groups[venue] = venue_groups.get(venue, Decimal("0")) + abs(notional)
        venue_concentration_complete = not missing_venues
        if venue_concentration_complete and venue_groups:
            venue_concentration = max(venue_groups.values()) / gross

    participation = Decimal("0")
    participation_evidenced = True
    if policy.max_order_participation_fraction is not None:
        liquidity_map = context.liquidity_capacity or {}
        capacity = liquidity_map.get(intent.symbol)
        if capacity is None or capacity <= 0:
            participation_evidenced = False
        else:
            participation = intent.quantity / capacity

    spread_observation = (context.spread_fraction or {}).get(intent.symbol)
    slippage_observation = (context.slippage_fraction or {}).get(intent.symbol)

    factor_exposure = Decimal("0")
    base_factor_exposure = Decimal("0")
    factor_exposure_complete = True
    missing_factor_loadings: set[str] = set()
    if policy.max_abs_factor_exposure is not None:
        loading_map = context.factor_loadings or {}
        projected_factors: dict[str, Decimal] = {}
        base_factors: dict[str, Decimal] = {}
        for symbol, notional in notionals.items():
            symbol_loadings = loading_map.get(symbol)
            if not symbol_loadings:
                missing_factor_loadings.add(symbol)
                continue
            for factor, loading in symbol_loadings.items():
                projected_factors[factor] = (
                    projected_factors.get(factor, Decimal("0"))
                    + notional * loading
                )
        factor_exposure_complete = not missing_factor_loadings
        if factor_exposure_complete:
            factor_exposure = max(
                (abs(value) for value in projected_factors.values()),
                default=Decimal("0"),
            )
        for symbol, notional in base_notionals.items():
            symbol_loadings = loading_map.get(symbol)
            if not symbol_loadings:
                continue
            for factor, loading in symbol_loadings.items():
                base_factors[factor] = (
                    base_factors.get(factor, Decimal("0"))
                    + notional * loading
                )
        base_factor_exposure = max(
            (abs(value) for value in base_factors.values()),
            default=Decimal("0"),
        )

    stress_symbols = set(notionals)
    base_stress_symbols = set(base_notionals)
    stress_coverage_complete = bool(context.stress_scenarios) or not stress_symbols
    missing_stress_symbols: set[str] = set()
    missing_base_stress_symbols: set[str] = set()
    missing_stress_labels: set[str] = set()
    mismatched_stress_labels: set[str] = set()
    stress_regime_coverage_complete = True
    if policy.required_stress_scenario_labels is not None and stress_symbols:
        observed_labels = set(context.stress_scenario_labels)
        missing_stress_labels = (
            set(policy.required_stress_scenario_labels) - observed_labels
        )
        required_digests = dict(policy.required_stress_scenario_digests or ())
        observed_scenarios = {
            label: scenario
            for label, scenario in zip(
                context.stress_scenario_labels,
                context.stress_scenarios,
            )
        }
        mismatched_stress_labels = {
            label
            for label in policy.required_stress_scenario_labels
            if label in observed_scenarios
            and stress_scenario_digest(observed_scenarios[label])
            != required_digests[label]
        }
        stress_regime_coverage_complete = (
            not missing_stress_labels
            and not mismatched_stress_labels
            and len(context.stress_scenario_labels) == len(context.stress_scenarios)
        )
    for scenario in context.stress_scenarios:
        scenario_symbols = set(scenario)
        missing_stress_symbols.update(stress_symbols - scenario_symbols)
        missing_base_stress_symbols.update(base_stress_symbols - scenario_symbols)
    if missing_stress_symbols:
        stress_coverage_complete = False
    base_stress_comparison_complete = (
        not stress_symbols
        or not base_stress_symbols
        or (
            bool(context.stress_scenarios)
            and not missing_base_stress_symbols
        )
    )

    base_worst_stress_loss = Decimal("0")
    worst_stress_loss = Decimal("0")
    if stress_coverage_complete:
        for scenario in context.stress_scenarios:
            if base_stress_comparison_complete:
                base_pnl = sum(
                    (
                        notional * scenario[symbol]
                        for symbol, notional in base_notionals.items()
                    ),
                    Decimal("0"),
                )
                base_worst_stress_loss = max(base_worst_stress_loss, -base_pnl)
            pnl = sum(
                (
                    notional * scenario[symbol]
                    for symbol, notional in notionals.items()
                ),
                Decimal("0"),
            )
            worst_stress_loss = max(worst_stress_loss, -pnl)

    tail_coverage_complete = True
    tail_distribution_matches = True
    base_tail_comparison_complete = True
    missing_tail_symbols: set[str] = set()
    missing_base_tail_symbols: set[str] = set()
    expected_shortfall: Decimal | None = None
    base_expected_shortfall: Decimal | None = None
    if policy.max_expected_shortfall is not None:
        if stress_symbols:
            tail_distribution_matches = (
                bool(context.tail_scenarios)
                and tail_scenario_set_digest(context.tail_scenarios)
                == policy.required_tail_scenario_set_digest
            )
        tail_coverage_complete = (
            (bool(context.tail_scenarios) or not stress_symbols)
            and tail_distribution_matches
        )
        for scenario in context.tail_scenarios:
            scenario_symbols = set(scenario)
            missing_tail_symbols.update(stress_symbols - scenario_symbols)
            missing_base_tail_symbols.update(base_stress_symbols - scenario_symbols)
        if missing_tail_symbols:
            tail_coverage_complete = False
        base_tail_comparison_complete = (
            not stress_symbols
            or not base_stress_symbols
            or (
                bool(context.tail_scenarios)
                and not missing_base_tail_symbols
            )
        )
        tail_fraction = policy.expected_shortfall_tail_fraction
        assert tail_fraction is not None
        tail_count = (
            max(
                1,
                int(
                    (Decimal(len(context.tail_scenarios)) * tail_fraction)
                    .to_integral_value(rounding=ROUND_CEILING)
                ),
            )
            if context.tail_scenarios
            else 0
        )
        if tail_coverage_complete and stress_symbols:
            projected_losses = []
            for scenario in context.tail_scenarios:
                projected_pnl = sum(
                    (notional * scenario[symbol] for symbol, notional in notionals.items()),
                    Decimal("0"),
                )
                projected_losses.append(max(-projected_pnl, Decimal("0")))
            projected_tail = sorted(projected_losses, reverse=True)[:tail_count]
            expected_shortfall = sum(projected_tail, Decimal("0")) / Decimal(
                len(projected_tail)
            )
        elif tail_coverage_complete:
            expected_shortfall = Decimal("0")

        if not base_stress_symbols:
            base_expected_shortfall = Decimal("0")
        elif base_tail_comparison_complete and context.tail_scenarios:
            base_losses: list[Decimal] = []
            for scenario in context.tail_scenarios:
                base_pnl = sum(
                    (
                        notional * scenario[symbol]
                        for symbol, notional in base_notionals.items()
                    ),
                    Decimal("0"),
                )
                base_losses.append(max(-base_pnl, Decimal("0")))
            base_tail = sorted(base_losses, reverse=True)[:tail_count]
            base_expected_shortfall = sum(base_tail, Decimal("0")) / Decimal(
                len(base_tail)
            )

    reduces_absolute_exposure = (
        abs(resulting) < abs(base_position)
        and base_position * resulting >= 0
    )
    stress_nonworsening = (
        not stress_symbols
        or (
            stress_coverage_complete
            and base_stress_comparison_complete
            and worst_stress_loss <= base_worst_stress_loss
        )
    )
    tail_nonworsening = (
        policy.max_expected_shortfall is None
        or not stress_symbols
        or (
            tail_coverage_complete
            and base_tail_comparison_complete
            and expected_shortfall is not None
            and base_expected_shortfall is not None
            and expected_shortfall <= base_expected_shortfall
        )
    )
    protective_reduction = (
        intent.reduce_only
        and reduces_absolute_exposure
        and gross < base_gross
        and net <= base_net
        and stress_nonworsening
        and tail_nonworsening
    )

    rules: list[RiskRuleResult] = []

    def add(rule: str, passed: bool, observed, limit, reason: str) -> None:
        rules.append(RiskRuleResult(rule, passed, str(observed), str(limit), reason))

    add(
        "state_version",
        intent.expected_state_version == context.state_version,
        context.state_version,
        intent.expected_state_version,
        "authoritative state version must match the intent",
    )
    add(
        "capability",
        context.capability_allowed,
        context.capability_allowed,
        True,
        "account/instrument capability must be currently evidenced",
    )
    if policy.allowed_actions is not None:
        add(
            "allowed_action",
            intent.action in policy.allowed_actions,
            intent.action,
            ",".join(policy.allowed_actions),
            "intent action class must be explicitly permitted by risk policy",
        )
    if policy.require_settlement_evidence:
        add(
            "settlement",
            context.settlement_allowed is True,
            (
                context.settlement_allowed
                if context.settlement_allowed is not None
                else "UNKNOWN"
            ),
            True,
            "settlement state must affirmatively permit the requested action",
        )
    if (
        policy.require_option_exercise_evidence
        and intent.action == "EXERCISE"
    ):
        add(
            "option_deliverable",
            context.option_deliverable_verified is True,
            (
                context.option_deliverable_verified
                if context.option_deliverable_verified is not None
                else "UNKNOWN"
            ),
            True,
            "option exercise requires a verified current deliverable",
        )
        exercise_funding_known = (
            context.option_exercise_cash_required is not None
            and context.option_exercise_cash_available is not None
        )
        add(
            "option_exercise_funding",
            exercise_funding_known
            and context.option_exercise_cash_available
            >= context.option_exercise_cash_required,
            (
                context.option_exercise_cash_available
                if exercise_funding_known
                else "UNKNOWN"
            ),
            (
                context.option_exercise_cash_required
                if context.option_exercise_cash_required is not None
                else "UNKNOWN"
            ),
            "option exercise requires evidenced buying power for its obligation",
        )
    add(
        "market_freshness",
        context.market_data_age_seconds <= policy.max_data_age_seconds,
        context.market_data_age_seconds,
        policy.max_data_age_seconds,
        "market data must be fresh enough for admission",
    )
    if policy.max_clock_age_seconds is not None:
        add(
            "clock_freshness",
            context.clock_age_seconds is not None
            and context.clock_age_seconds <= policy.max_clock_age_seconds,
            context.clock_age_seconds if context.clock_age_seconds is not None else "UNKNOWN",
            policy.max_clock_age_seconds,
            "clock synchronization evidence must be fresh enough for admission",
        )
    fx_evidenced = bool(context.fx_age_seconds) or not context.fx_required
    stale_fx = max(context.fx_age_seconds.values(), default=Decimal("0"))
    add(
        "fx_freshness",
        fx_evidenced and stale_fx <= policy.max_fx_age_seconds,
        stale_fx if fx_evidenced else "UNKNOWN",
        policy.max_fx_age_seconds,
        "required FX inputs must be present and fresh enough for valuation",
    )
    add(
        "position_limit",
        abs(resulting) <= policy.max_abs_position or protective_reduction,
        abs(resulting),
        policy.max_abs_position,
        "resulting absolute position must stay within policy",
    )
    add(
        "single_notional",
        single_notional <= policy.max_single_notional or protective_reduction,
        single_notional,
        policy.max_single_notional,
        "single-instrument notional must stay within policy",
    )
    add(
        "gross_leverage",
        gross_leverage <= policy.max_gross_leverage or protective_reduction,
        gross_leverage,
        policy.max_gross_leverage,
        "gross leverage must stay within policy",
    )
    add(
        "net_leverage",
        net_leverage <= policy.max_net_leverage or protective_reduction,
        net_leverage,
        policy.max_net_leverage,
        "net leverage must stay within policy",
    )
    if policy.max_asset_concentration_fraction is not None:
        add(
            "asset_concentration",
            asset_concentration_complete
            and asset_concentration <= policy.max_asset_concentration_fraction,
            (
                asset_concentration
                if asset_concentration_complete
                else "MISSING:" + ",".join(sorted(missing_asset_buckets))
            ),
            policy.max_asset_concentration_fraction,
            "projected gross exposure by asset bucket must stay within policy",
        )
    if policy.max_venue_concentration_fraction is not None:
        add(
            "venue_concentration",
            venue_concentration_complete
            and venue_concentration <= policy.max_venue_concentration_fraction,
            (
                venue_concentration
                if venue_concentration_complete
                else "MISSING:" + ",".join(sorted(missing_venues))
            ),
            policy.max_venue_concentration_fraction,
            "projected gross exposure by venue must stay within policy",
        )
    if policy.max_order_participation_fraction is not None:
        add(
            "liquidity_participation",
            participation_evidenced
            and participation <= policy.max_order_participation_fraction,
            participation if participation_evidenced else "UNKNOWN",
            policy.max_order_participation_fraction,
            "order quantity must stay within evidenced liquidity participation policy",
        )
    if policy.max_spread_fraction is not None:
        add(
            "spread",
            spread_observation is not None
            and spread_observation <= policy.max_spread_fraction,
            spread_observation if spread_observation is not None else "UNKNOWN",
            policy.max_spread_fraction,
            "evidenced execution spread must stay within policy",
        )
    if policy.max_slippage_fraction is not None:
        add(
            "slippage",
            slippage_observation is not None
            and slippage_observation <= policy.max_slippage_fraction,
            slippage_observation if slippage_observation is not None else "UNKNOWN",
            policy.max_slippage_fraction,
            "evidenced execution slippage must stay within policy",
        )
    if policy.max_abs_factor_exposure is not None:
        add(
            "factor_exposure",
            factor_exposure_complete
            and (
                factor_exposure <= policy.max_abs_factor_exposure
                or (
                    protective_reduction
                    and factor_exposure < base_factor_exposure
                )
            ),
            (
                factor_exposure
                if factor_exposure_complete
                else "MISSING:" + ",".join(sorted(missing_factor_loadings))
            ),
            policy.max_abs_factor_exposure,
            "correlated factor exposure must stay within the independent policy bound",
        )
    daily_loss = max(-context.daily_pnl, Decimal("0"))
    add(
        "daily_loss",
        daily_loss <= policy.max_daily_loss or protective_reduction,
        daily_loss,
        policy.max_daily_loss,
        "daily loss must stay within policy",
    )
    add(
        "drawdown",
        context.drawdown_fraction <= policy.max_drawdown_fraction or protective_reduction,
        context.drawdown_fraction,
        policy.max_drawdown_fraction,
        "drawdown must stay within policy",
    )
    add(
        "margin_headroom",
        context.margin_headroom >= policy.min_margin_headroom or protective_reduction,
        context.margin_headroom,
        policy.min_margin_headroom,
        "margin headroom must meet policy floor",
    )
    add(
        "stress_coverage",
        stress_coverage_complete,
        ",".join(sorted(missing_stress_symbols)) if missing_stress_symbols else len(context.stress_scenarios),
        "complete non-empty stress evidence for every non-zero projected position",
        "stress admission must fail closed when scenarios are missing or incomplete",
    )
    if policy.required_stress_scenario_labels is not None:
        add(
            "stress_regime_coverage",
            stress_regime_coverage_complete,
            (
                "NO_PROJECTED_RISK"
                if not stress_symbols
                else (
                    "MISSING:" + ",".join(sorted(missing_stress_labels))
                    if missing_stress_labels
                    else (
                        "MISMATCH:" + ",".join(sorted(mismatched_stress_labels))
                        if mismatched_stress_labels
                        else ",".join(context.stress_scenario_labels)
                    )
                )
            ),
            ",".join(policy.required_stress_scenario_labels),
            "configured frozen stress regimes must all be evidenced by labeled scenarios",
        )
    add(
        "stress_loss",
        stress_coverage_complete
        and (worst_stress_loss <= policy.max_stress_loss or protective_reduction),
        worst_stress_loss if stress_coverage_complete else "UNKNOWN",
        policy.max_stress_loss,
        "worst configured stress loss must stay within policy",
    )
    if policy.max_expected_shortfall is not None:
        add(
            "tail_coverage",
            tail_coverage_complete,
            (
                ",".join(sorted(missing_tail_symbols))
                if missing_tail_symbols
                else (
                    "DISTRIBUTION_MISMATCH"
                    if not tail_distribution_matches
                    else len(context.tail_scenarios)
                )
            ),
            "complete non-empty tail scenarios for every non-zero projected position",
            "expected-shortfall admission requires complete frozen tail evidence",
        )
        add(
            "expected_shortfall",
            tail_coverage_complete
            and expected_shortfall is not None
            and (
                expected_shortfall <= policy.max_expected_shortfall
                or protective_reduction
            ),
            expected_shortfall if expected_shortfall is not None else "UNKNOWN",
            policy.max_expected_shortfall,
            "equal-weight expected shortfall over the configured worst tail must stay within policy",
        )
    if policy.min_liquidation_headroom is not None:
        liquidation_evidence_verified = _verify_liquidation_headroom_evidence(
            evidence=context.liquidation_headroom_evidence,
            expected_scope=context.liquidation_scope,
            expected_state_version=context.state_version,
            decision_time=context.decision_time,
            evidence_store=evidence_store,
        )
        add(
            "liquidation_headroom",
            liquidation_evidence_verified
            and context.liquidation_headroom is not None
            and (
                context.liquidation_headroom >= policy.min_liquidation_headroom
                or protective_reduction
            ),
            (
                context.liquidation_headroom
                if liquidation_evidence_verified
                and context.liquidation_headroom is not None
                else (
                    "UNVERIFIED"
                    if context.liquidation_headroom is not None
                    else "UNKNOWN"
                )
            ),
            policy.min_liquidation_headroom,
            (
                "scope-bound immutable liquidation evidence must verify before "
                "the headroom floor or protective-reduction exception can admit risk"
            ),
        )

    opens_short = resulting < 0 and resulting < base_position
    borrow_ok = not opens_short or context.borrow_available is True
    add(
        "short_borrow",
        borrow_ok,
        context.borrow_available,
        True,
        "new or increased short exposure requires affirmative borrow evidence",
    )

    if (
        policy.min_futures_delivery_headroom_seconds is not None
        and intent.instrument_type == "FUTURE"
    ):
        delivery_headroom = (context.futures_delivery_headroom_seconds or {}).get(
            intent.symbol
        )
        delivery_ok = protective_reduction or (
            delivery_headroom is not None
            and delivery_headroom >= policy.min_futures_delivery_headroom_seconds
        )
        add(
            "futures_delivery_cutoff",
            delivery_ok,
            (
                "RISK_REDUCTION"
                if protective_reduction
                else (
                    delivery_headroom
                    if delivery_headroom is not None
                    else "UNKNOWN"
                )
            ),
            policy.min_futures_delivery_headroom_seconds,
            "new futures risk requires sufficient evidenced delivery headroom",
        )

    reduce_only_ok = not intent.reduce_only or reduces_absolute_exposure
    add(
        "reduce_only",
        reduce_only_ok,
        abs(resulting),
        abs(base_position),
        "reduce-only intent must not increase actual portfolio exposure",
    )

    return RiskDecision(
        admitted=all(rule.passed for rule in rules),
        resulting_position=resulting,
        gross_leverage=gross_leverage,
        net_leverage=net_leverage,
        worst_stress_loss=worst_stress_loss,
        input_fingerprint=_risk_input_fingerprint(intent, context, policy),
        rules=tuple(rules),
    )
