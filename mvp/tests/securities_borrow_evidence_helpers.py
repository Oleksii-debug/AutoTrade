from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.securities_borrow import (
    BORROW_PROVIDER_EVIDENCE_MEDIA_TYPE,
    DurableBorrowRecallProjection,
    provider_borrow_evidence_metadata,
    provider_borrow_evidence_receipt,
)
from research.autotrade_research.artifacts.store import ArtifactStore


def artifact_store_for(store: JournalStore) -> ArtifactStore:
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    return ArtifactStore(Path(store.path).parent / "borrow-provider-artifacts")


def bind_provider_evidence(artifact_store: ArtifactStore, evidence):
    receipt = provider_borrow_evidence_receipt(evidence)
    raw = canonical_json(receipt).encode("utf-8")
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/securities-borrow/"
            + canonical_json(receipt),
        )
    )
    manifest = artifact_store.publish_bytes(
        artifact_id=artifact_id,
        data=raw,
        media_type=BORROW_PROVIDER_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=[
            "provider-test:"
            + str(provider_borrow_evidence_metadata(evidence)["provider_revision"])
        ],
        metadata=provider_borrow_evidence_metadata(evidence),
    )
    return replace(
        evidence,
        evidence_ref=f"artifact:{artifact_id}@{manifest['sha256']}",
    )


class EvidencedBorrowRecallProjection:
    """Test-only evidence binder around the sealed production projection."""

    def __init__(self, store: JournalStore, **scope):
        self.store = store
        self._projection = DurableBorrowRecallProjection(
            store,
            evidence_artifact_store=artifact_store_for(store),
            **scope,
        )

    def _bound(self, evidence):
        if (
            type(getattr(evidence, "evidence_ref", None)) is str
            and evidence.evidence_ref.startswith("artifact:")
        ):
            return evidence
        return bind_provider_evidence(artifact_store_for(self.store), evidence)

    @property
    def version(self):
        return self._projection.version

    @property
    def active_quantity(self):
        return self._projection.active_quantity

    @property
    def active_recall_ids(self):
        return self._projection.active_recall_ids

    @property
    def active_blocking_resources(self):
        return self._projection.active_blocking_resources

    @property
    def resource_key(self):
        return self._projection.resource_key

    def remaining(self, recall_id):
        return self._projection.remaining(recall_id)

    def remaining_at(self, recall_id, now):
        return self._projection.remaining_at(recall_id, now)

    def active_quantity_at(self, now):
        return self._projection.active_quantity_at(now)

    def active_recall_ids_at(self, now):
        return self._projection.active_recall_ids_at(now)

    def active_blocking_resources_at(self, now):
        return self._projection.active_blocking_resources_at(now)

    def record_recall(self, evidence):
        return self._projection.record_recall(self._bound(evidence))

    def resolve_recall(self, evidence):
        return self._projection.resolve_recall(self._bound(evidence))

    def project_equity_state(self, state):
        return self._projection.project_equity_state(state)
