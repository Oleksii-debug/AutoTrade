from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.borrow import (
    BORROW_PROVIDER_EVIDENCE_MEDIA_TYPE,
    BorrowLifecycleJournal,
    provider_borrow_evidence_metadata,
    provider_borrow_evidence_receipt,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from research.autotrade_research.artifacts.store import ArtifactStore


def artifact_store_for(store: JournalStore) -> ArtifactStore:
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    return ArtifactStore(Path(store.path).parent / "provider-evidence-artifacts")


def bind_provider_evidence(
    artifact_store: ArtifactStore,
    evidence,
    *,
    artifact_id: str | None = None,
):
    """Publish one exact provider receipt and bind the evidence to its digest."""

    receipt = provider_borrow_evidence_receipt(evidence)
    raw = canonical_json(receipt).encode("utf-8")
    identity = artifact_id or str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/borrow/"
            + canonical_json(receipt),
        )
    )
    manifest = artifact_store.publish_bytes(
        artifact_id=identity,
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
        evidence_refs=(f"artifact:{identity}@{manifest['sha256']}",),
    )


class EvidencedBorrowJournal(BorrowLifecycleJournal):
    """Test fixture that publishes provider receipts before journal mutation."""

    def __init__(self, store: JournalStore, resource):
        super().__init__(
            store,
            resource,
            evidence_artifact_store=artifact_store_for(store),
        )

    def _bound(self, evidence):
        refs = getattr(evidence, "evidence_refs", ())
        if refs and all(
            isinstance(ref, str) and ref.startswith("artifact:")
            for ref in refs
        ):
            return evidence
        return bind_provider_evidence(artifact_store_for(self.store), evidence)

    def record_locate(self, evidence):
        return super().record_locate(self._bound(evidence))

    def record_loan(self, evidence):
        return super().record_loan(self._bound(evidence))

    def record_recall(self, evidence):
        return super().record_recall(self._bound(evidence))

    def record_recall_resolution(self, evidence):
        return super().record_recall_resolution(self._bound(evidence))
