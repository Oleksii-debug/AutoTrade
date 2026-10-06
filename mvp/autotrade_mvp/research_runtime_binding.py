"""Product-side binding between verified runtime cuts and research fold authority.

This module is deliberately placed in the product composition layer.  The
research package remains unaware of MVP/runtime verifier types and therefore
cannot mint or select a runtime trust root.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping

from autotrade_research.artifacts import ArtifactStore
from autotrade_research.data.vintages import HistoricalVintageRegistry
from autotrade_research.features.authoritative import (
    AuthoritativeFoldNormalizer,
    HistoricalFeatureInputSpec,
    fit_authoritative_fold_normalizer,
)
from autotrade_research.features.causal import CausalFold, FeaturePoint

from .replay import CompositeReplayCheckpoint, RuntimeStateVerifier


def _verified_runtime_cut(
    checkpoint: CompositeReplayCheckpoint,
    verifier: RuntimeStateVerifier,
) -> CompositeReplayCheckpoint:
    """Detach and verify one product-selected canonical runtime checkpoint."""

    if type(checkpoint) is not CompositeReplayCheckpoint:
        raise TypeError("checkpoint must be exact CompositeReplayCheckpoint")
    if type(verifier) is not RuntimeStateVerifier:
        raise TypeError("verifier must be exact RuntimeStateVerifier")

    # Detach caller-owned object identity before verification.  Frozen
    # dataclasses are still mutable through object.__setattr__, so downstream
    # research must receive a fingerprint from the exact bytes that were
    # actually verified rather than from the caller object after verification.
    document = CompositeReplayCheckpoint.to_canonical_json(checkpoint)
    canonical = CompositeReplayCheckpoint.from_canonical_json(document)
    RuntimeStateVerifier.verify_checkpoint_binding(verifier, canonical)
    return canonical


def fit_authoritative_fold_at_verified_runtime_cut(
    *,
    checkpoint: CompositeReplayCheckpoint,
    verifier: RuntimeStateVerifier,
    registry: HistoricalVintageRegistry,
    dataset_id: str,
    dataset_version: int,
    manifest_digest: str,
    artifact_store: ArtifactStore,
    fold: CausalFold,
    spec: HistoricalFeatureInputSpec,
    events: Iterable[Mapping[str, object]] | None = None,
) -> AuthoritativeFoldNormalizer:
    """Fit only after the canonical runtime checkpoint passes product trust."""

    verified = _verified_runtime_cut(checkpoint, verifier)
    return fit_authoritative_fold_normalizer(
        registry=registry,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        manifest_digest=manifest_digest,
        artifact_store=artifact_store,
        fold=fold,
        spec=spec,
        events=events,
        replay_common_cut_fingerprint=verified.fingerprint,
    )


def transform_authoritative_validation_at_verified_runtime_cut(
    normalizer: AuthoritativeFoldNormalizer,
    point: FeaturePoint,
    *,
    checkpoint: CompositeReplayCheckpoint,
    verifier: RuntimeStateVerifier,
    fold: CausalFold,
    registry: HistoricalVintageRegistry,
    artifact_store: ArtifactStore,
    spec: HistoricalFeatureInputSpec,
    events: Iterable[Mapping[str, object]] | None = None,
):
    """Transform validation data only at the exact verified fitted runtime cut."""

    if type(normalizer) is not AuthoritativeFoldNormalizer:
        raise TypeError("normalizer must be exact AuthoritativeFoldNormalizer")
    verified = _verified_runtime_cut(checkpoint, verifier)
    if normalizer.replay_common_cut_fingerprint != verified.fingerprint:
        raise ValueError(
            "authoritative fold normalizer belongs to a different verified runtime cut"
        )
    return AuthoritativeFoldNormalizer.transform_validation(
        normalizer,
        point,
        fold=fold,
        registry=registry,
        artifact_store=artifact_store,
        spec=spec,
        events=events,
        replay_common_cut_fingerprint=verified.fingerprint,
    )
