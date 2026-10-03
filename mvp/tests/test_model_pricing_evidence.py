from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_call import (
    DurableModelCallOrchestrator,
    ModelCallError,
    ModelCallSpec,
)
from mvp.autotrade_mvp.model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RoutingMode,
    RoutingPolicy,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.model_pricing_evidence import (
    LOCAL_RUNTIME_SOURCE,
    MODEL_PRICING_EVIDENCE_TYPE,
    MODEL_PRICING_MEDIA_TYPE,
    MODEL_PRICING_SCHEMA_VERSION,
    REMOTE_PROVIDER_SOURCE,
    ModelPricingEvidenceAuthority,
    ModelPricingEvidenceError,
    PricingAuthorityScope,
    TrustedPricingArtifactReceipt,
)


AS_OF = "2026-09-25T10:00:00Z"
VALID_UNTIL = "2026-09-25T11:00:00Z"
SOURCE = "sha256:" + "a" * 64


def descriptor(*, provider="provider-a", model="model-a", revision="r1", remote=True, cost="1.2"):
    return ModelDescriptor(
        model_id=model,
        provider_id=provider,
        revision=revision,
        remote=remote,
        estimated_cost=Decimal(cost),
        latency_ms=100,
        quality_score=Decimal("0.8"),
    )


def spec(artifact_id):
    return ModelCallSpec(
        job_id="job-1",
        input_digest="sha256:" + "1" * 64,
        policy_id="policy-v1",
        pricing_evidence_id=artifact_id,
        pricing_as_of=AS_OF,
        result_schema_id="schema-v1",
        cost_currency="USD",
    )


def body(*, source_class=REMOTE_PROVIDER_SOURCE, issuer="pricing-issuer", quotes=None):
    return {
        "schema_version": MODEL_PRICING_SCHEMA_VERSION,
        "evidence_type": MODEL_PRICING_EVIDENCE_TYPE,
        "issuer": issuer,
        "source_class": source_class,
        "source_sha256": SOURCE,
        "as_of": AS_OF,
        "valid_until": VALID_UNTIL,
        "cost_currency": "USD",
        "quotes": quotes or [
            {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "revision": "r1",
                "estimated_cost": "1.2",
            }
        ],
    }


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def publish(store, value, *, artifact_id=None, media_type=MODEL_PRICING_MEDIA_TYPE, metadata=None):
    raw = canonical(value)
    artifact_id = artifact_id or str(
        uuid5(NAMESPACE_URL, "pricing:" + sha256(raw).hexdigest())
    )
    expected_metadata = {
        "schema_version": MODEL_PRICING_SCHEMA_VERSION,
        "evidence_type": MODEL_PRICING_EVIDENCE_TYPE,
        "issuer": value["issuer"],
        "source_class": value["source_class"],
        "source_sha256": value["source_sha256"],
        "as_of": value["as_of"],
        "valid_until": value["valid_until"],
        "cost_currency": value["cost_currency"],
    }
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=raw,
        media_type=media_type,
        rights={"storage": True, "export": False},
        source_refs=[value["source_sha256"]],
        metadata=metadata if metadata is not None else expected_metadata,
    )
    return artifact_id, manifest, raw


def receipt(artifact_id, manifest, *, source_class=REMOTE_PROVIDER_SOURCE, issuer="pricing-issuer", scopes=None):
    return TrustedPricingArtifactReceipt(
        artifact_id=artifact_id,
        object_sha256=manifest["sha256"],
        source_sha256=SOURCE,
        issuer=issuer,
        source_class=source_class,
        scopes=scopes or (
            PricingAuthorityScope(
                provider_id="provider-a",
                model_id="model-a",
                revision="r1",
                remote=source_class == REMOTE_PROVIDER_SOURCE,
            ),
        ),
    )


class ModelPricingEvidenceAuthorityTests(unittest.TestCase):
    def test_valid_remote_artifact_resolves_immutable_digest_and_quote(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "pricing")
            artifact_id, manifest, _raw = publish(store, body())
            authority = ModelPricingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest),),
            )
            resolved = authority(spec(artifact_id), (descriptor(),))
            self.assertEqual(resolved.evidence_id, artifact_id)
            self.assertEqual(resolved.evidence_digest, manifest["sha256"])
            self.assertEqual(resolved.quotes[0].estimated_cost, Decimal("1.2"))

    def test_instance_split_reads_are_not_pricing_authority(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "pricing")
            artifact_id, manifest, _raw = publish(store, body())
            authority = ModelPricingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest),),
            )
            store.load_manifest = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split manifest read")
            )
            store.read_bytes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split payload read")
            )
            resolved = authority(spec(artifact_id), (descriptor(),))
            self.assertEqual(resolved.evidence_digest, manifest["sha256"])

    def test_untrusted_artifact_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "pricing")
            artifact_id, _manifest, _raw = publish(store, body())
            other_id, other_manifest, _ = publish(
                store,
                body(issuer="other-issuer"),
            )
            authority = ModelPricingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(
                        other_id,
                        other_manifest,
                        issuer="other-issuer",
                    ),
                ),
            )
            with self.assertRaisesRegex(
                ModelPricingEvidenceError,
                "no independently trusted issuer receipt",
            ):
                authority(spec(artifact_id), (descriptor(),))

    def test_wrong_media_or_metadata_fails_closed(self):
        with TemporaryDirectory() as directory:
            for wrong_media, wrong_metadata in (
                ("application/json", None),
                (
                    MODEL_PRICING_MEDIA_TYPE,
                    {
                        "schema_version": 1,
                        "evidence_type": MODEL_PRICING_EVIDENCE_TYPE,
                        "issuer": "pricing-issuer",
                        "source_class": REMOTE_PROVIDER_SOURCE,
                        "source_sha256": SOURCE,
                        "as_of": AS_OF,
                        "valid_until": VALID_UNTIL,
                        "cost_currency": "EUR",
                    },
                ),
            ):
                with self.subTest(media=wrong_media, metadata=wrong_metadata):
                    store = ArtifactStore(Path(directory) / uuid5(NAMESPACE_URL, str((wrong_media, wrong_metadata))).hex)
                    artifact_id, manifest, _ = publish(
                        store,
                        body(),
                        media_type=wrong_media,
                        metadata=wrong_metadata,
                    )
                    authority = ModelPricingEvidenceAuthority(
                        evidence_root=store.root,
                        publication_store=store,
                        trusted_receipts=(receipt(artifact_id, manifest),),
                    )
                    with self.assertRaises(ModelPricingEvidenceError):
                        authority(spec(artifact_id), (descriptor(),))

    def test_noncanonical_json_bytes_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "pricing")
            value = body()
            raw = json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
            artifact_id = "22222222-2222-4222-8222-222222222222"
            metadata = {
                "schema_version": MODEL_PRICING_SCHEMA_VERSION,
                "evidence_type": MODEL_PRICING_EVIDENCE_TYPE,
                "issuer": value["issuer"],
                "source_class": value["source_class"],
                "source_sha256": value["source_sha256"],
                "as_of": value["as_of"],
                "valid_until": value["valid_until"],
                "cost_currency": value["cost_currency"],
            }
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=raw,
                media_type=MODEL_PRICING_MEDIA_TYPE,
                rights={"storage": True, "export": False},
                source_refs=[SOURCE],
                metadata=metadata,
            )
            authority = ModelPricingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest),),
            )
            with self.assertRaisesRegex(
                ModelPricingEvidenceError,
                "canonical JSON",
            ):
                authority(spec(artifact_id), (descriptor(),))

    def test_noncanonical_float_negative_and_duplicate_quotes_fail(self):
        bad_quote_sets = (
            [
                {
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "revision": "r1",
                    "estimated_cost": 1.2,
                }
            ],
            [
                {
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "revision": "r1",
                    "estimated_cost": "-1",
                }
            ],
            [
                {
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "revision": "r1",
                    "estimated_cost": "1.2",
                },
                {
                    "provider_id": "provider-a",
                    "model_id": "model-a",
                    "revision": "r1",
                    "estimated_cost": "1.2",
                },
            ],
        )
        with TemporaryDirectory() as directory:
            for index, quotes in enumerate(bad_quote_sets):
                with self.subTest(index=index):
                    store = ArtifactStore(Path(directory) / str(index))
                    artifact_id, manifest, _ = publish(store, body(quotes=quotes))
                    scopes = (
                        PricingAuthorityScope("provider-a", "model-a", "r1", True),
                    )
                    authority = ModelPricingEvidenceAuthority(
                        evidence_root=store.root,
                        publication_store=store,
                        trusted_receipts=(
                            receipt(artifact_id, manifest, scopes=scopes),
                        ),
                    )
                    with self.assertRaises(ModelPricingEvidenceError):
                        authority(spec(artifact_id), (descriptor(),))

    def test_receipt_scope_cannot_be_widened_by_artifact(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "pricing")
            value = body(
                quotes=[
                    {
                        "provider_id": "provider-a",
                        "model_id": "model-a",
                        "revision": "r1",
                        "estimated_cost": "1.2",
                    },
                    {
                        "provider_id": "provider-b",
                        "model_id": "model-b",
                        "revision": "r2",
                        "estimated_cost": "2",
                    },
                ]
            )
            artifact_id, manifest, _ = publish(store, value)
            authority = ModelPricingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest),),
            )
            with self.assertRaisesRegex(
                ModelPricingEvidenceError,
                "trusted issuer scope",
            ):
                authority(spec(artifact_id), (descriptor(),))

    def test_local_zero_cost_evidence_cannot_authorize_remote_descriptor(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "pricing")
            value = body(
                source_class=LOCAL_RUNTIME_SOURCE,
                quotes=[
                    {
                        "provider_id": "local-runtime",
                        "model_id": "local-model",
                        "revision": "build-1",
                        "estimated_cost": "0",
                    }
                ],
            )
            artifact_id, manifest, _ = publish(store, value)
            local_scope = PricingAuthorityScope(
                "local-runtime", "local-model", "build-1", False
            )
            authority = ModelPricingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(
                    receipt(
                        artifact_id,
                        manifest,
                        source_class=LOCAL_RUNTIME_SOURCE,
                        scopes=(local_scope,),
                    ),
                ),
            )
            local = descriptor(
                provider="local-runtime",
                model="local-model",
                revision="build-1",
                remote=False,
                cost="0",
            )
            self.assertEqual(
                authority(spec(artifact_id), (local,)).quotes[0].estimated_cost,
                Decimal("0"),
            )
            forged_remote = descriptor(
                provider="local-runtime",
                model="local-model",
                revision="build-1",
                remote=True,
                cost="0",
            )
            with self.assertRaisesRegex(
                ModelPricingEvidenceError,
                "remote/local class",
            ):
                authority(spec(artifact_id), (forged_remote,))

    def test_descriptor_cost_mismatch_fails_before_durable_reservation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "pricing")
            artifact_id, manifest, _ = publish(store, body())
            authority = ModelPricingEvidenceAuthority(
                evidence_root=store.root,
                publication_store=store,
                trusted_receipts=(receipt(artifact_id, manifest),),
            )
            budget = DurableModelBudget(
                journal=JournalStore(root / "journal.sqlite3"),
                budget_id="pricing-integration-budget",
                ceiling="5",
                environment="PAPER",
                clock=lambda: AS_OF,
            )
            orchestrator = DurableModelCallOrchestrator(
                budget=budget,
                clock=lambda: AS_OF,
                pricing_evidence_resolver=authority,
                observation_evidence_resolver=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("observation resolver must not run")
                ),
                billing_evidence_resolver=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("billing resolver must not run")
                ),
            )
            call_spec = spec(artifact_id)
            attempt_id = orchestrator.attempt_id(call_spec)
            request = ModelRequest(
                request_id=attempt_id,
                allowed_model_ids=("model-a",),
                privacy_remote_allowed=True,
                budget_remaining=Decimal("5"),
                deadline_utc=datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc),
            )
            policy = RoutingPolicy(
                mode=RoutingMode.FIXED,
                allowed_model_ids=("model-a",),
                fixed_model_id="model-a",
                allow_remote=True,
                maximum_cost=Decimal("2"),
            )
            conflicting = descriptor(cost="1.3")
            with self.assertRaisesRegex(
                ModelCallError,
                "descriptor estimated cost conflicts",
            ):
                orchestrator.execute(
                    spec=call_spec,
                    policy=policy,
                    request=request,
                    descriptors=(conflicting,),
                    call=lambda *_args: (_ for _ in ()).throw(
                        AssertionError("model call must not run")
                    ),
                    validate_result=lambda _value: True,
                    now_utc=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                )
            self.assertIsNone(budget.active_reservation(attempt_id))

    def test_configured_root_cannot_be_redirected_to_caller_store(self):
        with TemporaryDirectory() as directory:
            root_a = Path(directory) / "authoritative"
            root_b = Path(directory) / "caller"
            store_a = ArtifactStore(root_a)
            store_b = ArtifactStore(root_b)
            artifact_id = "11111111-1111-4111-8111-111111111111"
            _id_a, manifest_a, _ = publish(
                store_a,
                body(),
                artifact_id=artifact_id,
            )
            publish(
                store_b,
                body(
                    quotes=[
                        {
                            "provider_id": "provider-a",
                            "model_id": "model-a",
                            "revision": "r1",
                            "estimated_cost": "0",
                        }
                    ]
                ),
                artifact_id=artifact_id,
            )
            authority = ModelPricingEvidenceAuthority(
                evidence_root=root_a,
                publication_store=store_a,
                trusted_receipts=(receipt(artifact_id, manifest_a),),
            )
            resolved = authority(spec(artifact_id), (descriptor(),))
            self.assertEqual(resolved.quotes[0].estimated_cost, Decimal("1.2"))
            self.assertEqual(resolved.evidence_digest, manifest_a["sha256"])


if __name__ == "__main__":
    unittest.main()
