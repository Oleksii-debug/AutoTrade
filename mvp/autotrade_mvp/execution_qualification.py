"""Fail-closed qualification boundary for execution simulation models.

This module does not simulate orders itself and does not create execution
authority.  It binds an already implemented deterministic execution model to a
specific asset class, data fidelity, scenario, scientific protocol and
resolved immutable artifact evidence before the model may be used as qualified
replay evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal
from uuid import UUID

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .execution_oracle import assert_conservative_execution
from .exact_decimal import ExactDecimalError, is_exact_decimal_multiple
from .instruments import (
    InstrumentRegistryError,
    InstrumentVersion,
    _detached_instrument_version,
)
from .execution_realism import (
    ExecutionModel,
    ExecutionPriceProjectionPolicy,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedExecution,
    SimulatedOrder,
    _detached_dataclass_input,
    _execution_price_grid_matches_instrument,
    _instant,
    simulate_execution,
)


class ExecutionQualificationError(ValueError):
    pass


_ASSET_CLASSES = {
    "CASH_EQUITY",
    "FUND",
    "FX",
    "CRYPTO_SPOT",
    "FUTURE",
    "PERPETUAL",
    "OPTION",
}
_PURPOSES = {"RESEARCH", "REPLAY", "PROMOTION"}


def _validate_instrument_metadata_authority(
    *,
    instrument: InstrumentVersion,
    artifact_store: ArtifactStore,
    knowledge_cutoff: str | None,
) -> None:
    """Authenticate the exact InstrumentVersion facts behind execution rules.

    Use the one installed neutral ArtifactStore and generation-bound reader
    directly. Research compatibility imports alias that same authority, while
    installed-runtime qualification must work when the research package is
    absent. Keep the immutable instrument-metadata contract unchanged.
    """

    cutoff = (
        datetime.max.replace(tzinfo=timezone.utc)
        if knowledge_cutoff is None
        else _instant(knowledge_cutoff, name="instrument_knowledge_cutoff")
    )
    if not instrument.metadata_evidence:
        raise ExecutionQualificationError(
            "qualified execution requires authenticated instrument metadata evidence"
        )

    try:
        trusted_read = trusted_authenticated_reader(
            artifact_store.root,
            publication_store=artifact_store,
        )
        binding = instrument.metadata_evidence_binding()
        known_at = []
        for evidence in instrument.metadata_evidence:
            artifact_id = evidence["artifact_id"]
            expected_digest = evidence["sha256"]
            observed_raw = evidence["observed_at"]
            manifest, _data = trusted_read(artifact_id)

            if manifest.get("sha256") != expected_digest:
                raise ExecutionQualificationError(
                    "instrument metadata evidence digest mismatch"
                )
            if (
                manifest.get("media_type")
                != "application/vnd.autotrade.instrument-metadata+json"
            ):
                raise ExecutionQualificationError(
                    "instrument metadata evidence media type is invalid"
                )
            metadata = manifest.get("metadata")
            if (
                type(metadata) is not dict
                or metadata.get("kind") != "instrument-metadata"
                or metadata.get("instrument_version_binding") != binding
            ):
                raise ExecutionQualificationError(
                    "instrument metadata evidence is not bound to this instrument version"
                )
            if (
                "rights_id" in evidence
                and manifest.get("rights", {}).get("rights_id")
                != evidence["rights_id"]
            ):
                raise ExecutionQualificationError(
                    "instrument metadata evidence rights identity mismatch"
                )

            observed = datetime.fromisoformat(
                observed_raw[:-1] + "+00:00"
            ).astimezone(timezone.utc)
            committed_raw = manifest.get("created_at")
            if type(committed_raw) is not str:
                raise ExecutionQualificationError(
                    "instrument metadata evidence lacks trusted commit time"
                )
            committed = datetime.fromisoformat(
                committed_raw.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if observed > committed:
                raise ExecutionQualificationError(
                    "instrument metadata evidence observation follows immutable commit"
                )
            known_at.append(max(observed, committed))
    except ExecutionQualificationError:
        raise
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        raise ExecutionQualificationError(
            "instrument metadata evidence cannot be authenticated"
        ) from error

    if max(known_at) > cutoff:
        raise ExecutionQualificationError(
            "qualified execution requires authenticated instrument metadata evidence"
        )

def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise ExecutionQualificationError(f"{name} is required")
    text = value.strip()
    if not text:
        raise ExecutionQualificationError(f"{name} is required")
    return text


def _uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        return str(UUID(text))
    except (ValueError, AttributeError, TypeError) as error:
        raise ExecutionQualificationError(f"{name} must be a UUID") from error


def _sha256(value: object, *, name: str) -> str:
    text = _text(value, name=name).lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64:
        raise ExecutionQualificationError(f"{name} must be a SHA-256 digest")
    try:
        int(text, 16)
    except ValueError as error:
        raise ExecutionQualificationError(f"{name} must be hexadecimal") from error
    return text


@dataclass(frozen=True, slots=True)
class ExecutionModelQualification:
    qualification_id: str
    asset_class: str
    data_fidelity: Literal["BAR", "TOP_OF_BOOK", "BOOK"]
    scenario: Literal["OPTIMISTIC", "BASE", "ADVERSE"]
    purpose: Literal["RESEARCH", "REPLAY", "PROMOTION"]
    model_fingerprint: str
    calibration_sha256: str
    protocol_sha256: str
    evidence_artifact_id: str
    evidence_sha256: str
    instrument_version: str | None = None

    def __post_init__(self) -> None:
        qualification_id = _text(self.qualification_id, name="qualification_id")
        asset_class = _text(self.asset_class, name="asset_class").upper()
        if asset_class not in _ASSET_CLASSES:
            raise ExecutionQualificationError("unsupported asset_class")
        fidelity = _text(self.data_fidelity, name="data_fidelity").upper()
        if fidelity not in {"BAR", "TOP_OF_BOOK", "BOOK"}:
            raise ExecutionQualificationError("unsupported data_fidelity")
        scenario = _text(self.scenario, name="scenario").upper()
        if scenario not in {"OPTIMISTIC", "BASE", "ADVERSE"}:
            raise ExecutionQualificationError("unsupported scenario")
        purpose = _text(self.purpose, name="purpose").upper()
        if purpose not in _PURPOSES:
            raise ExecutionQualificationError("unsupported purpose")
        if purpose == "PROMOTION" and scenario == "OPTIMISTIC":
            raise ExecutionQualificationError(
                "OPTIMISTIC scenario cannot qualify promotion evidence"
            )

        fingerprint = _sha256(self.model_fingerprint, name="model_fingerprint")
        calibration = _sha256(self.calibration_sha256, name="calibration_sha256")
        protocol = _sha256(self.protocol_sha256, name="protocol_sha256")
        evidence_artifact_id = _uuid(
            self.evidence_artifact_id,
            name="evidence_artifact_id",
        )
        evidence = _sha256(self.evidence_sha256, name="evidence_sha256")
        instrument = (
            None
            if self.instrument_version is None
            else _text(self.instrument_version, name="instrument_version")
        )

        object.__setattr__(self, "qualification_id", qualification_id)
        object.__setattr__(self, "asset_class", asset_class)
        object.__setattr__(self, "data_fidelity", fidelity)
        object.__setattr__(self, "scenario", scenario)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "model_fingerprint", fingerprint)
        object.__setattr__(self, "calibration_sha256", calibration)
        object.__setattr__(self, "protocol_sha256", protocol)
        object.__setattr__(self, "evidence_artifact_id", evidence_artifact_id)
        object.__setattr__(self, "evidence_sha256", evidence)
        object.__setattr__(self, "instrument_version", instrument)


def validate_execution_qualification(
    *,
    model: ExecutionModel,
    qualification: ExecutionModelQualification,
    asset_class: str,
    instrument_version: str,
    instrument: InstrumentVersion,
    protocol_sha256: str,
    artifact_store: ArtifactStore,
    evidence_artifact_id: str,
    purpose: str,
    instrument_knowledge_cutoff: str | None = None,
) -> None:
    """Fail closed unless every frozen qualification dimension matches exactly."""

    if type(model) is not ExecutionModel:
        raise TypeError("model must be exact ExecutionModel")
    model = _detached_dataclass_input(model, ExecutionModel, name="model")
    if type(qualification) is not ExecutionModelQualification:
        raise TypeError("qualification must be exact ExecutionModelQualification")
    qualification_values = {}
    for field in fields(ExecutionModelQualification):
        value = getattr(qualification, field.name)
        if value is not None and type(value) is not str:
            raise TypeError(
                f"qualification.{field.name} must be exact str"
            )
        qualification_values[field.name] = value
    qualification = ExecutionModelQualification(**qualification_values)
    if type(artifact_store) is not ArtifactStore:
        raise TypeError("artifact_store must be the canonical ArtifactStore")

    normalized_asset = _text(asset_class, name="asset_class").upper()
    if normalized_asset not in _ASSET_CLASSES:
        raise ExecutionQualificationError("unsupported asset_class")
    normalized_instrument = _text(
        instrument_version,
        name="instrument_version",
    )
    if type(instrument) is not InstrumentVersion:
        raise TypeError("instrument must be exact InstrumentVersion")
    detached_instrument = _detached_instrument_version(instrument)
    authoritative_instrument = (
        f"{detached_instrument.instrument_id}@{detached_instrument.version}"
    )
    authoritative_projection = ExecutionPriceProjectionPolicy.from_instrument(
        detached_instrument
    )
    normalized_purpose = _text(purpose, name="purpose").upper()
    if normalized_purpose not in _PURPOSES:
        raise ExecutionQualificationError("unsupported purpose")
    normalized_protocol = _sha256(protocol_sha256, name="protocol_sha256")
    normalized_evidence_artifact_id = _uuid(
        evidence_artifact_id,
        name="evidence_artifact_id",
    )

    try:
        evidence_manifest, evidence_bytes = ArtifactStore.read_authenticated_snapshot(
            artifact_store,
            normalized_evidence_artifact_id,
        )
    except (FileNotFoundError, ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
        raise ExecutionQualificationError(
            "execution evidence artifact cannot be verified"
        ) from error

    resolved_evidence = _sha256(
        evidence_manifest.get("sha256"),
        name="resolved evidence sha256",
    )
    if sha256(evidence_bytes).hexdigest() != resolved_evidence:
        raise ExecutionQualificationError(
            "execution evidence bytes differ from authenticated manifest"
        )

    failures: list[str] = []
    if normalized_instrument != authoritative_instrument:
        failures.append("instrument_authority")
    if normalized_asset != detached_instrument.asset_class:
        failures.append("instrument_asset_class")
    if model.price_projection != authoritative_projection:
        failures.append("price_projection_authority")
    if detached_instrument.metadata_evidence:
        try:
            price_grid_matches = (
                model.price_grid is not None
                and _execution_price_grid_matches_instrument(
                    model.price_grid,
                    detached_instrument,
                )
            )
        except (ExecutionRealismError, TypeError, ValueError):
            price_grid_matches = False
        if not price_grid_matches:
            failures.append("price_grid_authority")
    if qualification.asset_class != normalized_asset:
        failures.append("asset_class")
    if qualification.data_fidelity != model.data_fidelity:
        failures.append("data_fidelity")
    if qualification.scenario != model.scenario:
        failures.append("scenario")
    if qualification.purpose != normalized_purpose:
        failures.append("purpose")
    if qualification.model_fingerprint != model.fingerprint:
        failures.append("model_fingerprint")
    if qualification.calibration_sha256 != model.calibration_sha256:
        failures.append("calibration_sha256")
    if qualification.protocol_sha256 != normalized_protocol:
        failures.append("protocol_sha256")
    if qualification.evidence_artifact_id != normalized_evidence_artifact_id:
        failures.append("evidence_artifact_id")
    if qualification.evidence_sha256 != resolved_evidence:
        failures.append("evidence_sha256")
    if (
        qualification.instrument_version is not None
        and qualification.instrument_version != normalized_instrument
    ):
        failures.append("instrument_version")

    if normalized_purpose == "PROMOTION" and model.scenario == "OPTIMISTIC":
        failures.append("optimistic_promotion")

    if failures:
        raise ExecutionQualificationError(
            "execution model is not qualified for requested use: "
            + ", ".join(sorted(set(failures)))
        )

    _validate_instrument_metadata_authority(
        instrument=detached_instrument,
        artifact_store=artifact_store,
        knowledge_cutoff=instrument_knowledge_cutoff,
    )


def _validate_qualified_instrument_rules(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    instrument: InstrumentVersion,
) -> None:
    """Bind qualified order/liquidity scalars to canonical instrument rules."""

    if type(instrument) is not InstrumentVersion:
        raise TypeError("instrument must be exact InstrumentVersion")
    detached = _detached_instrument_version(instrument)
    failures: list[str] = []

    try:
        lot_is_on_step = is_exact_decimal_multiple(
            order.lot_size,
            detached.quantity_step,
        )
    except ExactDecimalError as error:
        raise ExecutionQualificationError(
            "qualified lot-size check exceeds exact arithmetic resource envelope"
        ) from error
    if not lot_is_on_step:
        failures.append("instrument_quantity_step")

    try:
        detached.validate_quantity(order.quantity)
    except InstrumentRegistryError:
        failures.append("instrument_quantity_rules")

    submitted_at = _instant(order.submitted_at, name="order.submitted_at")
    market_time = _instant(observation.market_time, name="observation.market_time")
    if not detached.contains(submitted_at):
        failures.append("instrument_effective_at_order")
    if not detached.contains(market_time):
        failures.append("instrument_effective_at_market")

    for field_name in ("limit_price", "stop_price"):
        price = getattr(order, field_name)
        if price is None:
            continue
        try:
            detached.validate_price(price)
        except InstrumentRegistryError:
            failures.append(f"instrument_{field_name}_rules")

    for field_name in ("bid", "ask", "bar_high", "bar_low"):
        price = getattr(observation, field_name)
        if price is None:
            continue
        try:
            detached.validate_price(price)
        except InstrumentRegistryError:
            failures.append(f"instrument_{field_name}_rules")

    if failures:
        raise ExecutionQualificationError(
            "qualified execution violates canonical instrument rules: "
            + ", ".join(sorted(set(failures)))
        )


def simulate_qualified_execution(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
    qualification: ExecutionModelQualification,
    instrument: InstrumentVersion,
    asset_class: str,
    protocol_sha256: str,
    artifact_store: ArtifactStore,
    evidence_artifact_id: str,
    purpose: str = "REPLAY",
) -> SimulatedExecution:
    """Run the existing simulator only after exact qualification succeeds.

    The returned execution remains simulation evidence.  This function creates
    no provider credentials, admission, confirmation or live trading authority.
    """

    order = _detached_dataclass_input(order, SimulatedOrder, name="order")
    observation = _detached_dataclass_input(
        observation,
        LiquidityObservation,
        name="observation",
    )
    model = _detached_dataclass_input(model, ExecutionModel, name="model")

    validate_execution_qualification(
        model=model,
        qualification=qualification,
        asset_class=asset_class,
        instrument_version=order.instrument_version,
        instrument=instrument,
        protocol_sha256=protocol_sha256,
        artifact_store=artifact_store,
        evidence_artifact_id=evidence_artifact_id,
        purpose=purpose,
        instrument_knowledge_cutoff=order.submitted_at,
    )
    _validate_qualified_instrument_rules(
        order=order,
        observation=observation,
        instrument=instrument,
    )
    result = simulate_execution(order, observation, model)
    assert_conservative_execution(
        order=order,
        observation=observation,
        model=model,
        result=result,
    )
    return result
