"""Test-only authenticated projection support for provider-Q consumer tests.

Production consumers deliberately require the exact
DurableProviderQualificationRegistry type. Older route tests used a subclass
that bypassed durable evidence authentication, so those tests could not exercise
the production type boundary they claimed to cover.

This helper keeps the production object exact and patches only the canonical
campaign-verification function for the lifetime of one unit test. Registered
records still have to match the exact protocol key, signed receipt, scope and
release-artifact identity requested by durable replay. It is unit-test support
only and is never provider-qualification evidence.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.durable_provider_qualification import (
    DurableProviderQualificationRegistry,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_qualification_authority import (
    AcceptedProviderQualification,
    ProviderQualificationError,
)
from mvp.autotrade_mvp.qualification_attestation import SignedQualificationAttestation


class ExactQualificationProjectionHarness:
    """Keep consumer tests on the exact production registry type."""

    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str],
            tuple[AcceptedProviderQualification, SignedQualificationAttestation],
        ] = {}
        self._patcher = patch(
            "mvp.autotrade_mvp.durable_provider_qualification."
            "verify_provider_qualification_campaign",
            side_effect=self._verify,
        )
        self._started = False

    def start(self) -> "ExactQualificationProjectionHarness":
        if self._started:
            raise RuntimeError("projection harness is already started")
        self._patcher.start()
        self._started = True
        return self

    def stop(self) -> None:
        if self._started:
            self._patcher.stop()
            self._started = False

    def registry(
        self,
        store: JournalStore,
        *,
        evidence_store: ArtifactStore,
        evidence_root: str | Path,
    ) -> DurableProviderQualificationRegistry:
        if not self._started:
            raise RuntimeError("projection harness must be started first")
        return DurableProviderQualificationRegistry(
            store,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
        )

    def register(
        self,
        *,
        protocol_key: str,
        record: AcceptedProviderQualification,
        receipt: SignedQualificationAttestation,
    ) -> None:
        if not self._started:
            raise RuntimeError("projection harness must be started first")
        key = (protocol_key, receipt.attestation.attestation_id)
        existing = self._entries.get(key)
        value = (record, receipt)
        if existing is not None and existing != value:
            raise AssertionError("projection test identity was registered twice")
        self._entries[key] = value

    def _verify(
        self,
        *,
        protocol_key,
        receipt,
        evidence_store,
        evidence_root,
        expected_scope,
        expected_release_artifact_id,
    ):
        del evidence_store, evidence_root
        if type(protocol_key) is not str:
            raise ProviderQualificationError(
                "projection verifier requires canonical protocol key"
            )
        if type(receipt) is not SignedQualificationAttestation:
            raise ProviderQualificationError(
                "projection verifier requires exact signed receipt"
            )
        key = (protocol_key, receipt.attestation.attestation_id)
        try:
            record, registered_receipt = self._entries[key]
        except KeyError as error:
            raise ProviderQualificationError(
                "projection verifier has no registered qualification"
            ) from error
        if receipt != registered_receipt:
            raise ProviderQualificationError(
                "projection verifier receipt differs from registered receipt"
            )
        if record.scope != expected_scope:
            raise ProviderQualificationError(
                "projection verifier scope differs from durable record"
            )
        if record.release_artifact_id != expected_release_artifact_id:
            raise ProviderQualificationError(
                "projection verifier release identity differs from durable record"
            )
        return record
