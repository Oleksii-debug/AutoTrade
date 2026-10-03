from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.model_billing_evidence import (
    LOCAL_RUNTIME_METER_SOURCE,
    MODEL_BILLING_EVIDENCE_TYPE,
    MODEL_BILLING_MEDIA_TYPE,
    MODEL_BILLING_SCHEMA_VERSION,
    REMOTE_PROVIDER_BILLING_SOURCE,
    ModelBillingEvidenceAuthority,
    ModelBillingEvidenceError,
    TrustedBillingArtifactReceipt,
)


SOURCE = "sha256:" + "a" * 64
PRICING = "sha256:" + "b" * 64
OBSERVATION = "sha256:" + "c" * 64
ATTEMPT = "model-attempt-" + "d" * 64
BILLING = "invoice-line-1"
OBSERVED_AT = "2026-09-25T10:05:00Z"


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def scope(*, terminal_state="OBSERVED", remote=True, provider="provider-a"):
    observed = terminal_state == "OBSERVED"
    return {
        "attempt_id": ATTEMPT,
        "request_id": ATTEMPT,
        "request_identity_digest": "sha256:" + "1" * 64,
        "budget_id": "budget-a",
        "environment": "PAPER",
        "provider_id": provider,
        "model_id": "model-a",
        "revision": "r1",
        "remote": remote,
        "pricing_evidence_id": "pricing-a",
        "pricing_evidence_digest": PRICING,
        "cost_currency": "USD",
        "terminal_state": terminal_state,
        "provider_request_id": "request-7" if observed and remote else None,
        "provider_response_id": "response-7" if observed and remote else None,
        "usage_id": "usage-7" if observed else None,
        "observation_digest": OBSERVATION if observed else None,
        "observation_evidence_id": "observation-artifact" if observed else None,
        "observation_evidence_digest": (
            "sha256:" + "2" * 64 if observed else None
        ),
        "observation_evidence_issuer": "provider-a" if observed else None,
    }


def body(
    *,
    billed="0.25",
    terminal_state="OBSERVED",
    source_class=REMOTE_PROVIDER_BILLING_SOURCE,
    provider="provider-a",
):
    observed = terminal_state == "OBSERVED"
    return {
        "schema_version": MODEL_BILLING_SCHEMA_VERSION,
        "evidence_type": MODEL_BILLING_EVIDENCE_TYPE,
        "issuer": "billing-issuer",
        "source_class": source_class,
        "source_sha256": SOURCE,
        "attempt_id": ATTEMPT,
        "billing_id": BILLING,
        "provider_id": provider,
        "model_id": "model-a",
        "revision": "r1",
        "cost_currency": "USD",
        "pricing_evidence_digest": PRICING,
        "terminal_state": terminal_state,
        "provider_request_id": (
            "request-7" if observed and remote_source else None
        ),
        "provider_response_id": (
            "response-7" if observed and remote_source else None
        ),
        "usage_id": "usage-7" if observed else None,
        "observation_digest": OBSERVATION if observed else None,
        "billed": billed,
        "observed_at": OBSERVED_AT,
    }


def metadata(value):
    return {
        key: value[key]
        for key in (
            "schema_version",
            "evidence_type",
            "issuer",
            "source_class",
            "source_sha256",
            "attempt_id",
            "billing_id",
            "provider_id",
            "model_id",
            "revision",
            "cost_currency",
            "pricing_evidence_digest",
            "terminal_state",
            "provider_request_id",
            "provider_response_id",
            "usage_id",
            "observation_digest",
            "billed",
            "observed_at",
        )
    }


def publish(store, value, *, artifact_id=None, media_type=MODEL_BILLING_MEDIA_TYPE, meta=None):
    raw = canonical(value)
    artifact_id = artifact_id or str(
        uuid5(NAMESPACE_URL, "billing:" + sha256(raw).hexdigest())
    )
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=raw,
        media_type=media_type,
        rights={"storage": True, "export": False},
        source_refs=[value["source_sha256"]],
        metadata=metadata(value) if meta is None else meta,
    )
    return artifact_id, manifest, raw


def receipt(
    artifact_id,
    manifest,
    *,
    value=None,
    source_class=REMOTE_PROVIDER_BILLING_SOURCE,
):
    value = value or body(source_class=source_class)
    return TrustedBillingArtifactReceipt(
        artifact_id=artifact_id,
        object_sha256=manifest["sha256"],
        source_sha256=value["source_sha256"],
        issuer=value["issuer"],
        source_class=value["source_class"],
        attempt_id=value["attempt_id"],
        billing_id=value["billing_id"],
        provider_id=value["provider_id"],
        model_id=value["model_id"],
        revision=value["revision"],
        cost_currency=value["cost_currency"],
        pricing_evidence_digest=value["pricing_evidence_digest"],
        terminal_state=value["terminal_state"],
        provider_request_id=value["provider_request_id"],
        provider_response_id=value["provider_response_id"],
        usage_id=value["usage_id"],
        observation_digest=value["observation_digest"],
    )


class ModelBillingEvidenceAuthorityTests(unittest.TestCase):
    def test_valid_observed_invoice_derives_amount_from_authenticated_bytes(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body(billed="0.25")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelBillingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest, value=value),),
            )
            evidence = authority(ATTEMPT, BILLING, scope())
            self.assertEqual(evidence.billed, Decimal("0.25"))
            self.assertEqual(evidence.evidence_id, artifact_id)
            self.assertEqual(evidence.evidence_digest, manifest["sha256"])

    def test_authenticated_reader_is_single_snapshot_authority(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body()
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelBillingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest, value=value),),
            )
            store.load_manifest = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split manifest read")
            )
            store.read_bytes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split payload read")
            )
            self.assertEqual(
                authority(ATTEMPT, BILLING, scope()).billed,
                Decimal("0.25"),
            )

    def test_untrusted_billing_identity_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body()
            artifact_id, manifest, _ = publish(store, value)
            trusted = receipt(artifact_id, manifest, value=value)
            authority = ModelBillingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(trusted,),
            )
            with self.assertRaisesRegex(
                ModelBillingEvidenceError,
                "no independently trusted issuer receipt",
            ):
                authority(ATTEMPT, "other-invoice", scope())

    def test_durable_scope_mismatch_fails_before_amount_acceptance(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body()
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelBillingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest, value=value),),
            )
            forged = dict(scope())
            forged["pricing_evidence_digest"] = "sha256:" + "f" * 64
            with self.assertRaisesRegex(
                ModelBillingEvidenceError,
                "does not match durable model-call scope",
            ):
                authority(ATTEMPT, BILLING, forged)

    def test_manifest_metadata_cannot_rewrite_invoice_amount(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body(billed="0.25")
            wrong = metadata(value)
            wrong["billed"] = "0.01"
            artifact_id, manifest, _ = publish(store, value, meta=wrong)
            authority = ModelBillingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest, value=value),),
            )
            with self.assertRaisesRegex(
                ModelBillingEvidenceError,
                "metadata scope mismatch",
            ):
                authority(ATTEMPT, BILLING, scope())

    def test_noncanonical_or_negative_amount_fails_closed(self):
        for billed in ("0.250", "-0.1"):
            with self.subTest(billed=billed), TemporaryDirectory() as directory:
                store = ArtifactStore(Path(directory) / "billing")
                value = body(billed=billed)
                artifact_id, manifest, _ = publish(store, value)
                authority = ModelBillingEvidenceAuthority(
                    evidence_root=store.root,
                    publication_store=store,
                    trusted_receipts=(receipt(artifact_id, manifest, value=value),),
                )
                with self.assertRaisesRegex(
                    ModelBillingEvidenceError,
                    "billed must be",
                ):
                    authority(ATTEMPT, BILLING, scope())

    def test_unknown_invoice_line_binds_attempt_without_inventing_response_lineage(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body(terminal_state="UNKNOWN")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelBillingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest, value=value),),
            )
            evidence = authority(
                ATTEMPT,
                BILLING,
                scope(terminal_state="UNKNOWN"),
            )
            self.assertEqual(evidence.billed, Decimal("0.25"))

    def test_unknown_receipt_cannot_claim_observed_response_identity(self):
        value = body(terminal_state="UNKNOWN")
        value["provider_response_id"] = "invented-response"
        with self.assertRaisesRegex(
            ModelBillingEvidenceError,
            "UNKNOWN billing receipt cannot claim",
        ):
            TrustedBillingArtifactReceipt(
                artifact_id="11111111-1111-4111-8111-111111111111",
                object_sha256="sha256:" + "1" * 64,
                source_sha256=SOURCE,
                issuer="billing-issuer",
                source_class=REMOTE_PROVIDER_BILLING_SOURCE,
                attempt_id=ATTEMPT,
                billing_id=BILLING,
                provider_id="provider-a",
                model_id="model-a",
                revision="r1",
                cost_currency="USD",
                pricing_evidence_digest=PRICING,
                terminal_state="UNKNOWN",
                provider_response_id="invented-response",
            )

    def test_local_runtime_cannot_mint_nonzero_external_billing(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body(
                billed="0.01",
                source_class=LOCAL_RUNTIME_METER_SOURCE,
                provider="local-runtime",
            )
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelBillingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest, value=value),),
            )
            with self.assertRaisesRegex(
                ModelBillingEvidenceError,
                "exact zero external cost",
            ):
                authority(
                    ATTEMPT,
                    BILLING,
                    scope(remote=False, provider="local-runtime"),
                )

    def test_local_runtime_receipt_cannot_claim_remote_provider_ids(self):
        with self.assertRaisesRegex(
            ModelBillingEvidenceError,
            "cannot claim remote provider identities",
        ):
            TrustedBillingArtifactReceipt(
                artifact_id="22222222-2222-4222-8222-222222222222",
                object_sha256="sha256:" + "3" * 64,
                source_sha256=SOURCE,
                issuer="local-meter",
                source_class=LOCAL_RUNTIME_METER_SOURCE,
                attempt_id=ATTEMPT,
                billing_id=BILLING,
                provider_id="local-runtime",
                model_id="model-a",
                revision="r1",
                cost_currency="USD",
                pricing_evidence_digest=PRICING,
                terminal_state="OBSERVED",
                provider_request_id="remote-request",
                observation_digest=OBSERVATION,
            )

    def test_duplicate_trusted_billing_identity_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "billing")
            value = body()
            artifact_id, manifest, _ = publish(store, value)
            trusted = receipt(artifact_id, manifest, value=value)
            with self.assertRaisesRegex(
                ModelBillingEvidenceError,
                "duplicate trusted billing identity",
            ):
                ModelBillingEvidenceAuthority(
                    evidence_root=store.root,
                    publication_store=store,
                    trusted_receipts=(trusted, trusted),
                )


if __name__ == "__main__":
    unittest.main()
