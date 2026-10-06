from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.model_call import ModelCallBinding, ModelCallObservation
from mvp.autotrade_mvp.model_observation_evidence import (
    LOCAL_RUNTIME_METER_SOURCE,
    MODEL_OBSERVATION_EVIDENCE_TYPE,
    MODEL_OBSERVATION_MEDIA_TYPE,
    MODEL_OBSERVATION_SCHEMA_VERSION,
    REMOTE_PROVIDER_RESPONSE_SOURCE,
    ModelObservationEvidenceAuthority,
    ModelObservationEvidenceError,
    TrustedObservationArtifactReceipt,
)
from mvp.autotrade_mvp.persistence import payload_digest


SOURCE = "sha256:" + "a" * 64
PRICING = "sha256:" + "b" * 64
ATTEMPT = "model-attempt-" + "c" * 64
OBSERVED_AT = "2026-09-25T10:04:00Z"


def binding(*, remote=True, provider="provider-a", attempt_id=ATTEMPT):
    return ModelCallBinding(
        attempt_id=attempt_id,
        request_id=ATTEMPT,
        job_id="job-a",
        input_digest="sha256:" + "1" * 64,
        provider_id=provider,
        model_id="model-a",
        revision="r1",
        remote=remote,
        reserved_cost=Decimal("1.2"),
        pricing_evidence_id="pricing-a",
        pricing_evidence_digest=PRICING,
        pricing_as_of="2026-09-25T10:00:00Z",
        cost_currency="USD",
        result_schema_id="schema-a",
        fallback_parent_attempt_id=None,
        fallback_index=0,
    )


def observation(*, remote=True, provider="provider-a", output=None):
    return ModelCallObservation(
        provider_id=provider,
        model_id="model-a",
        revision="r1",
        observed_at=OBSERVED_AT,
        incurred_cost=Decimal("0.3"),
        estimated_unbilled=Decimal("0.4"),
        output={"answer": 7} if output is None else output,
        provider_request_id="request-7" if remote else None,
        provider_response_id="response-7" if remote else None,
        usage_id="usage-7",
        billing_id="invoice-line-7",
    )


def observation_digest(value, call_binding):
    return payload_digest(
        {
            "attempt_id": call_binding.attempt_id,
            "provider_id": value.provider_id,
            "model_id": value.model_id,
            "revision": value.revision,
            "observed_at": value.observed_at,
            "incurred_cost": str(value.incurred_cost),
            "estimated_unbilled": str(value.estimated_unbilled),
            "output_digest": payload_digest(value.output),
            "provider_request_id": value.provider_request_id,
            "provider_response_id": value.provider_response_id,
            "usage_id": value.usage_id,
            "billing_id": value.billing_id,
        }
    )


def body(value, call_binding, *, source_class=REMOTE_PROVIDER_RESPONSE_SOURCE):
    return {
        "schema_version": MODEL_OBSERVATION_SCHEMA_VERSION,
        "evidence_type": MODEL_OBSERVATION_EVIDENCE_TYPE,
        "issuer": "observation-issuer",
        "source_class": source_class,
        "source_sha256": SOURCE,
        "attempt_id": call_binding.attempt_id,
        "provider_id": value.provider_id,
        "model_id": value.model_id,
        "revision": value.revision,
        "pricing_evidence_digest": call_binding.pricing_evidence_digest,
        "cost_currency": call_binding.cost_currency,
        "observed_at": value.observed_at,
        "incurred_cost": str(value.incurred_cost),
        "estimated_unbilled": str(value.estimated_unbilled),
        "output_digest": payload_digest(value.output),
        "provider_request_id": value.provider_request_id,
        "provider_response_id": value.provider_response_id,
        "usage_id": value.usage_id,
        "billing_id": value.billing_id,
        "observation_digest": observation_digest(value, call_binding),
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
            "provider_id",
            "model_id",
            "revision",
            "pricing_evidence_digest",
            "cost_currency",
            "observed_at",
            "incurred_cost",
            "estimated_unbilled",
            "output_digest",
            "provider_request_id",
            "provider_response_id",
            "usage_id",
            "billing_id",
            "observation_digest",
        )
    }


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def publish(store, value, *, artifact_id=None, meta=None):
    raw = canonical(value)
    artifact_id = artifact_id or str(
        uuid5(NAMESPACE_URL, "observation:" + sha256(raw).hexdigest())
    )
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=raw,
        media_type=MODEL_OBSERVATION_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=[value["source_sha256"]],
        metadata=metadata(value) if meta is None else meta,
    )
    return artifact_id, manifest, raw


def receipt(artifact_id, manifest, value, call_binding):
    return TrustedObservationArtifactReceipt(
        artifact_id=artifact_id,
        object_sha256=manifest["sha256"],
        source_sha256=value["source_sha256"],
        issuer=value["issuer"],
        source_class=value["source_class"],
        attempt_id=value["attempt_id"],
        provider_id=value["provider_id"],
        model_id=value["model_id"],
        revision=value["revision"],
        pricing_evidence_digest=call_binding.pricing_evidence_digest,
        cost_currency=call_binding.cost_currency,
        provider_request_id=value["provider_request_id"],
        provider_response_id=value["provider_response_id"],
        usage_id=value["usage_id"],
        billing_id=value["billing_id"],
    )


class ModelObservationEvidenceAuthorityTests(unittest.TestCase):
    def test_valid_remote_response_resolves_exact_observation_digest(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            observed = observation()
            value = body(observed, call_binding)
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(artifact_id, manifest, value, call_binding),
                ),
            )
            evidence = authority(observed, call_binding)
            self.assertEqual(
                evidence.observation_digest,
                observation_digest(observed, call_binding),
            )
            self.assertEqual(evidence.evidence_digest, manifest["sha256"])

    def test_authenticated_reader_is_single_snapshot_authority(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            observed = observation()
            value = body(observed, call_binding)
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(artifact_id, manifest, value, call_binding),
                ),
            )
            store.load_manifest = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split manifest read")
            )
            store.read_bytes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split payload read")
            )
            self.assertEqual(
                authority(observed, call_binding).evidence_digest,
                manifest["sha256"],
            )

    def test_untrusted_attempt_fails_closed(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            observed = observation()
            value = body(observed, call_binding)
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            trusted = receipt(artifact_id, manifest, value, call_binding)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(trusted,),
            )
            other = binding(attempt_id="model-attempt-" + "e" * 64)
            with self.assertRaisesRegex(
                ModelObservationEvidenceError,
                "no independently trusted observation receipt",
            ):
                authority(observed, other)

    def test_binding_scope_mismatch_fails_before_artifact_acceptance(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            observed = observation()
            value = body(observed, call_binding)
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(artifact_id, manifest, value, call_binding),
                ),
            )
            wrong = binding(provider="provider-b")
            with self.assertRaisesRegex(
                ModelObservationEvidenceError,
                "does not match model-call binding",
            ):
                authority(observed, wrong)

    def test_output_digest_substitution_fails_closed(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            retained = observation()
            value = body(retained, call_binding)
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(artifact_id, manifest, value, call_binding),
                ),
            )
            substituted = observation(output={"answer": 8})
            with self.assertRaisesRegex(
                ModelObservationEvidenceError,
                "does not bind exact response/usage semantics",
            ):
                authority(substituted, call_binding)

    def test_manifest_metadata_cannot_rewrite_usage_identity(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            observed = observation()
            value = body(observed, call_binding)
            wrong = metadata(value)
            wrong["usage_id"] = "forged-usage"
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value, meta=wrong)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(artifact_id, manifest, value, call_binding),
                ),
            )
            with self.assertRaisesRegex(
                ModelObservationEvidenceError,
                "metadata scope mismatch",
            ):
                authority(observed, call_binding)

    def test_noncanonical_cost_fails_closed(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            observed = observation()
            value = body(observed, call_binding)
            value["incurred_cost"] = "0.300"
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(artifact_id, manifest, value, call_binding),
                ),
            )
            with self.assertRaisesRegex(
                ModelObservationEvidenceError,
                "incurred_cost must be",
            ):
                authority(observed, call_binding)

    def test_remote_receipt_requires_provider_request_and_response_ids(self):
        with self.assertRaisesRegex(
            ModelObservationEvidenceError,
            "requires request/response identities",
        ):
            TrustedObservationArtifactReceipt(
                artifact_id="33333333-3333-4333-8333-333333333333",
                object_sha256="sha256:" + "4" * 64,
                source_sha256=SOURCE,
                issuer="remote-provider",
                source_class=REMOTE_PROVIDER_RESPONSE_SOURCE,
                attempt_id=ATTEMPT,
                provider_id="provider-a",
                model_id="model-a",
                revision="r1",
                pricing_evidence_digest=PRICING,
                cost_currency="USD",
                provider_request_id=None,
                provider_response_id=None,
                usage_id="usage-7",
                billing_id="invoice-line-7",
            )

    def test_local_runtime_evidence_is_disjoint_from_remote_transport_identity(self):
        with self.assertRaisesRegex(
            ModelObservationEvidenceError,
            "cannot claim remote provider identities",
        ):
            TrustedObservationArtifactReceipt(
                artifact_id="11111111-1111-4111-8111-111111111111",
                object_sha256="sha256:" + "3" * 64,
                source_sha256=SOURCE,
                issuer="local-meter",
                source_class=LOCAL_RUNTIME_METER_SOURCE,
                attempt_id=ATTEMPT,
                provider_id="local-runtime",
                model_id="model-a",
                revision="r1",
                pricing_evidence_digest=PRICING,
                cost_currency="USD",
                provider_request_id="remote-request",
                provider_response_id=None,
                usage_id="local-usage",
                billing_id=None,
            )

    def test_valid_local_runtime_meter_cannot_authorize_remote_route(self):
        with TemporaryDirectory() as directory:
            local_binding = binding(remote=False, provider="local-runtime")
            local_observation = observation(remote=False, provider="local-runtime")
            value = body(
                local_observation,
                local_binding,
                source_class=LOCAL_RUNTIME_METER_SOURCE,
            )
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelObservationEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(artifact_id, manifest, value, local_binding),
                ),
            )
            self.assertEqual(
                authority(local_observation, local_binding).observation_digest,
                observation_digest(local_observation, local_binding),
            )
            with self.assertRaisesRegex(
                ModelObservationEvidenceError,
                "cannot cross remote/local route",
            ):
                authority(local_observation, binding())

    def test_duplicate_trusted_attempt_is_rejected(self):
        with TemporaryDirectory() as directory:
            call_binding = binding()
            observed = observation()
            value = body(observed, call_binding)
            store = ArtifactStore(Path(directory) / "observation")
            artifact_id, manifest, _ = publish(store, value)
            trusted = receipt(artifact_id, manifest, value, call_binding)
            with self.assertRaisesRegex(
                ModelObservationEvidenceError,
                "duplicate trusted observation attempt",
            ):
                ModelObservationEvidenceAuthority(
                    evidence_root=store.root,
                    publication_store=store,
                    trusted_receipts=(trusted, trusted),
                )


if __name__ == "__main__":
    unittest.main()
