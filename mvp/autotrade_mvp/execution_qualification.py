"""Fail-closed qualification boundary for execution simulation models.

This module does not simulate orders itself and does not create execution
authority.  It binds an already implemented deterministic execution model to a
specific asset class, data fidelity, scenario, scientific protocol and
resolved immutable artifact evidence before the model may be used as qualified
replay evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore

from .execution_oracle import assert_conservative_execution
from .execution_realism import (
    ExecutionModel,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedExecution,
    SimulatedOrder,
    simulate_execution,
)


class ExecutionQualificationError(ValueError):
    pass


_ASSET_CLASSES = {
    "SPOT",
    "MARGIN",
    "EQUITY",
    "FUTURE",
    "PERPETUAL",
    "OPTION",
}
_PURPOSES = {"RESEARCH", "REPLAY", "PROMOTION"}


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionQualificationError(f"{name} is required")
    return value.strip()


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
    protocol_sha256: str,
    artifact_store: ArtifactStore,
    evidence_artifact_id: str,
    purpose: str,
) -> None:
    """Fail closed unless every frozen qualification dimension matches exactly."""

    if not isinstance(model, ExecutionModel):
        raise TypeError("model must be ExecutionModel")
    if not isinstance(qualification, ExecutionModelQualification):
        raise TypeError("qualification must be ExecutionModelQualification")
    if not isinstance(artifact_store, ArtifactStore):
        raise TypeError("artifact_store must be the canonical ArtifactStore")

    normalized_asset = _text(asset_class, name="asset_class").upper()
    if normalized_asset not in _ASSET_CLASSES:
        raise ExecutionQualificationError("unsupported asset_class")
    normalized_instrument = _text(
        instrument_version,
        name="instrument_version",
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
        evidence_manifest = artifact_store.load_manifest(
            normalized_evidence_artifact_id
        )
        if evidence_manifest.get("manifest_hash") is None:
            raise ExecutionQualificationError(
                "execution evidence manifest lacks integrity binding"
            )
        artifact_store.read_bytes(normalized_evidence_artifact_id)
    except ExecutionQualificationError:
        raise
    except (FileNotFoundError, ArtifactIntegrityError, OSError, ValueError) as error:
        raise ExecutionQualificationError(
            "execution evidence artifact cannot be verified"
        ) from error

    resolved_evidence = _sha256(
        evidence_manifest.get("sha256"),
        name="resolved evidence sha256",
    )

    failures: list[str] = []
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


def simulate_qualified_execution(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
    qualification: ExecutionModelQualification,
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

    if not isinstance(order, SimulatedOrder):
        raise TypeError("order must be SimulatedOrder")
    if not isinstance(observation, LiquidityObservation):
        raise TypeError("observation must be LiquidityObservation")

    validate_execution_qualification(
        model=model,
        qualification=qualification,
        asset_class=asset_class,
        instrument_version=order.instrument_version,
        protocol_sha256=protocol_sha256,
        artifact_store=artifact_store,
        evidence_artifact_id=evidence_artifact_id,
        purpose=purpose,
    )
    result = simulate_execution(order, observation, model)
    assert_conservative_execution(
        order=order,
        observation=observation,
        model=model,
        result=result,
    )
    return result
