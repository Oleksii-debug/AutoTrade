import base64
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.performance_qualification import RuntimeLoadObservation
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationTrustError,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.runtime_load_campaign import (
    RuntimeLoadCampaignEvidence,
    serialize_runtime_load_campaign_evidence,
)
from mvp.autotrade_mvp.runtime_target_host_campaign import (
    COLLECTOR_ID as CAMPAIGN_COLLECTOR_ID,
    COLLECTOR_VERSION as CAMPAIGN_COLLECTOR_VERSION,
)
from mvp.autotrade_mvp.runtime_target_host_inventory import (
    COLLECTOR_ID as HOST_INVENTORY_COLLECTOR_ID,
    COLLECTOR_VERSION as HOST_INVENTORY_COLLECTOR_VERSION,
    RuntimeTargetHostInventory,
    host_identity_fingerprint,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    BINDING_EVIDENCE_KIND,
    CAMPAIGN_EVIDENCE_KIND,
    DOMAIN,
    GATE,
    HOST_INVENTORY_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    JSON_MEDIA_TYPE,
    PACKAGE_ID,
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    REQUIREMENT_ID,
    RESOURCE_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
    RuntimeTargetHostBinding,
    RuntimeTargetHostProvenance,
    RuntimeTargetHostQualificationError,
    verify_runtime_target_host_qualification,
)


SOURCE = "a" * 40
SPEC = "sha256:" + "b" * 64
CONFIG = "sha256:" + "c" * 64
HOST_IDENTITY = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.12.11",
    "cpu_count": 8,
}
HOST = host_identity_fingerprint(HOST_IDENTITY)
WORKLOAD = "sha256:" + "e" * 64
JOURNAL = "sha256:" + "f" * 64
RELEASE_ID = "20000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "5" * 64
ALT_RELEASE_ID = "20000000-0000-4000-8000-000000000002"
ALT_RELEASE_SHA = "sha256:" + "4" * 64
ROOT = "sha256:" + "6" * 64
ATTESTATION_DIGEST = "sha256:" + "7" * 64
POLICY = "sha256:" + "8" * 64

KINDS = (
    CAMPAIGN_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    HOST_INVENTORY_EVIDENCE_KIND,
)
_KIND_IDS = {
    BINDING_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000001",
    CAMPAIGN_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000002",
    STALENESS_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000003",
    INTERFERENCE_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000004",
    RESOURCE_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000005",
    HOST_INVENTORY_EVIDENCE_KIND: "00000000-0000-4000-8000-000000000006",
}
_PAYLOAD_IDS = {
    kind: f"30000000-0000-4000-8000-{index:012d}"
    for index, kind in enumerate(KINDS, start=1)
}


def _campaign_raw(
    *,
    source_sha: str = SOURCE,
    spec_digest: str = SPEC,
    configuration_hash: str = CONFIG,
    host_identity=None,
) -> bytes:
    identity = dict(HOST_IDENTITY if host_identity is None else host_identity)
    host_fingerprint = host_identity_fingerprint(identity)
    observation = RuntimeLoadObservation.create(
        scenario_id="target-host-primary",
        spec_digest=spec_digest,
        release_sha=source_sha,
        configuration_hash=configuration_hash,
        host_fingerprint=host_fingerprint,
        expected_financial_events=2,
        recovered_financial_events=2,
        financial_latency_us=(100, 200),
        financial_staleness_us=(),
        research_interference_us=(),
        reconnect_backlog_remaining=0,
        declared_duration_us=1_000,
        observed_duration_us=900,
    )
    evidence = RuntimeLoadCampaignEvidence(
        observation=observation,
        journal_sequence_before=10,
        journal_sequence_after=12,
        recovered_event_ids=("financial-1", "financial-2"),
        recovered_journal_sequences=(11, 12),
        host_identity=identity,
    )
    return serialize_runtime_load_campaign_evidence(evidence)


_RAW_PAYLOAD = {
    kind: f"retained-raw-evidence:{kind}".encode("utf-8")
    for kind in KINDS
}
_RAW_PAYLOAD[CAMPAIGN_EVIDENCE_KIND] = _campaign_raw()
_RAW_PAYLOAD[HOST_INVENTORY_EVIDENCE_KIND] = RuntimeTargetHostInventory(
    host_identity=HOST_IDENTITY,
    host_fingerprint=HOST,
).canonical_bytes()


def _sha(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


_PAYLOAD_DIGEST = {kind: _sha(raw) for kind, raw in _RAW_PAYLOAD.items()}


def provenance(kind: str, **overrides) -> RuntimeTargetHostProvenance:
    collector_id = f"collector-{kind.lower()}"
    collector_version = "1.0.0"
    if kind == CAMPAIGN_EVIDENCE_KIND:
        collector_id = CAMPAIGN_COLLECTOR_ID
        collector_version = CAMPAIGN_COLLECTOR_VERSION
    elif kind == HOST_INVENTORY_EVIDENCE_KIND:
        collector_id = HOST_INVENTORY_COLLECTOR_ID
        collector_version = HOST_INVENTORY_COLLECTOR_VERSION
    values = {
        "evidence_kind": kind,
        "source_sha": SOURCE,
        "scenario_id": "target-host-primary",
        "spec_digest": SPEC,
        "configuration_hash": CONFIG,
        "host_fingerprint": HOST,
        "workload_profile_hash": WORKLOAD,
        "journal_store_identity_digest": JOURNAL,
        "release_artifact_id": RELEASE_ID,
        "release_artifact_sha256": RELEASE_SHA,
        "collector_id": collector_id,
        "collector_version": collector_version,
        "payload_artifact_id": _PAYLOAD_IDS[kind],
        "payload_sha256": _PAYLOAD_DIGEST[kind],
    }
    values.update(overrides)
    return RuntimeTargetHostProvenance(**values)


def ref(kind: str, digest: str) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        artifact_id=_KIND_IDS[kind],
        sha256=digest,
        media_type=JSON_MEDIA_TYPE,
        evidence_kind=kind,
        source_sha=SOURCE,
    )


def material(
    *,
    provenance_overrides=None,
    binding_overrides=None,
    raw_payload_overrides=None,
):
    provenance_overrides = provenance_overrides or {}
    raw_payload_overrides = raw_payload_overrides or {}
    raw_by_id = {}
    evidence_refs = []
    digest_by_kind = {}
    for kind in KINDS:
        raw_payload = raw_payload_overrides.get(kind, _RAW_PAYLOAD[kind])
        overrides = dict(provenance_overrides.get(kind, {}))
        overrides.setdefault("payload_sha256", _sha(raw_payload))
        envelope = provenance(kind, **overrides)
        raw_by_id[envelope.payload_artifact_id] = raw_payload
        raw = envelope.canonical_bytes()
        digest = _sha(raw)
        digest_by_kind[kind] = digest
        raw_by_id[_KIND_IDS[kind]] = raw
        evidence_refs.append(ref(kind, digest))
    binding_values = {
        "source_sha": SOURCE,
        "scenario_id": "target-host-primary",
        "spec_digest": SPEC,
        "configuration_hash": CONFIG,
        "host_fingerprint": HOST,
        "workload_profile_hash": WORKLOAD,
        "journal_store_identity_digest": JOURNAL,
        "release_artifact_id": RELEASE_ID,
        "release_artifact_sha256": RELEASE_SHA,
        "campaign_evidence_sha256": digest_by_kind[CAMPAIGN_EVIDENCE_KIND],
        "staleness_evidence_sha256": digest_by_kind[STALENESS_EVIDENCE_KIND],
        "interference_evidence_sha256": digest_by_kind[INTERFERENCE_EVIDENCE_KIND],
        "resource_evidence_sha256": digest_by_kind[RESOURCE_EVIDENCE_KIND],
        "host_inventory_evidence_sha256": digest_by_kind[HOST_INVENTORY_EVIDENCE_KIND],
    }
    binding_values.update(binding_overrides or {})
    binding_value = RuntimeTargetHostBinding(**binding_values)
    raw_binding = binding_value.canonical_bytes()
    raw_by_id[_KIND_IDS[BINDING_EVIDENCE_KIND]] = raw_binding
    evidence_refs.append(ref(BINDING_EVIDENCE_KIND, _sha(raw_binding)))
    return raw_by_id, tuple(evidence_refs), binding_value


def receipt(evidence_refs) -> SignedQualificationAttestation:
    attestation = QualificationAttestation(
        attestation_id="10000000-0000-4000-8000-000000000001",
        source_sha=SOURCE,
        domain=DOMAIN,
        gate=GATE,
        package_id=PACKAGE_ID,
        protocol_id=PROTOCOL_ID,
        protocol_version=PROTOCOL_VERSION,
        requirement_ids=(REQUIREMENT_ID,),
        evidence_refs=tuple(evidence_refs),
        producer_id="runtime-qualification-producer",
        verifier_id="runtime-qualification-verifier",
        trust_root_id=ROOT,
        runner_id="target-host-runner",
        harness_version="1.0.0",
        started_at="2026-10-03T00:00:00Z",
        completed_at="2026-10-03T00:01:00Z",
        signed_at="2026-10-03T00:01:01Z",
        result="PASS",
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
    )
    return SignedQualificationAttestation(
        attestation=attestation,
        signature_b64=base64.b64encode(b"profile-unit-test-signature").decode("ascii"),
    )


def accepted(
    evidence_refs,
    *,
    result="PASS",
    unresolved_limits=(),
    release_artifact_id=RELEASE_ID,
    release_artifact_sha256=RELEASE_SHA,
):
    return AcceptedQualificationAttestation(
        attestation_id="10000000-0000-4000-8000-000000000001",
        attestation_digest=ATTESTATION_DIGEST,
        policy_id=POLICY,
        policy_version="1.0.0",
        trust_root_id=ROOT,
        result=result,
        source_sha=SOURCE,
        domain=DOMAIN,
        gate=GATE,
        package_id=PACKAGE_ID,
        protocol_id=PROTOCOL_ID,
        protocol_version=PROTOCOL_VERSION,
        requirement_id=REQUIREMENT_ID,
        requirement_ids=(REQUIREMENT_ID,),
        evidence_refs=tuple(evidence_refs),
        producer_id="runtime-qualification-producer",
        verifier_id="runtime-qualification-verifier",
        runner_id="target-host-runner",
        harness_version="1.0.0",
        started_at="2026-10-03T00:00:00Z",
        completed_at="2026-10-03T00:01:00Z",
        signed_at="2026-10-03T00:01:01Z",
        unresolved_limits=tuple(unresolved_limits),
        schema_version="1.0.0",
        verification_method="RSA_PKCS1V15_SHA256",
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
        attestation_json="{}",
        signature_b64="AA==",
    )


class RuntimeTargetHostQualificationTests(unittest.TestCase):
    def _verify(
        self,
        raw_by_id,
        evidence_refs,
        *,
        accepted_value=None,
        expected_release_artifact_id=RELEASE_ID,
        expected_release_artifact_sha256=RELEASE_SHA,
    ):
        signed = receipt(evidence_refs)
        accepted_value = accepted_value or accepted(evidence_refs)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")

            def reader(artifact_id):
                if artifact_id not in raw_by_id:
                    raise FileNotFoundError(artifact_id)
                return {}, raw_by_id[artifact_id]

            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                return_value=accepted_value,
            ) as canonical, patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
                return_value=reader,
            ):
                result = verify_runtime_target_host_qualification(
                    signed,
                    evidence_store=store,
                    evidence_root=directory,
                    expected_source_sha=SOURCE,
                    expected_scenario_id="target-host-primary",
                    expected_spec_digest=SPEC,
                    expected_configuration_hash=CONFIG,
                    expected_host_fingerprint=HOST,
                    expected_workload_profile_hash=WORKLOAD,
                    expected_journal_store_identity_digest=JOURNAL,
                    expected_release_artifact_id=expected_release_artifact_id,
                    expected_release_artifact_sha256=expected_release_artifact_sha256,
                )
            kwargs = canonical.call_args.kwargs
            self.assertEqual(kwargs["expected_domain"], DOMAIN)
            self.assertEqual(kwargs["expected_gate"], GATE)
            self.assertEqual(kwargs["expected_package_id"], PACKAGE_ID)
            self.assertEqual(kwargs["expected_protocol_id"], PROTOCOL_ID)
            self.assertEqual(kwargs["expected_requirement_id"], REQUIREMENT_ID)
            self.assertEqual(kwargs["expected_release_artifact_id"], RELEASE_ID)
            self.assertEqual(kwargs["expected_release_artifact_sha256"], RELEASE_SHA)
            return result

    def test_binding_and_provenance_round_trip_are_canonical(self):
        raw_by_id, _refs, binding_value = material()
        self.assertEqual(
            RuntimeTargetHostBinding.parse(binding_value.canonical_bytes()),
            binding_value,
        )
        raw = raw_by_id[_KIND_IDS[RESOURCE_EVIDENCE_KIND]]
        self.assertEqual(
            RuntimeTargetHostProvenance.parse(raw).evidence_kind,
            RESOURCE_EVIDENCE_KIND,
        )

    def test_duplicate_or_noncanonical_json_is_rejected(self):
        raw = (
            b'{"source_sha":"' + SOURCE.encode() +
            b'","source_sha":"' + SOURCE.encode() + b'"}'
        )
        with self.assertRaises(RuntimeTargetHostQualificationError):
            RuntimeTargetHostBinding.parse(raw)
        raw_by_id, _refs, binding_value = material()
        with self.assertRaises(RuntimeTargetHostQualificationError):
            RuntimeTargetHostBinding.parse(binding_value.canonical_bytes() + b"\n")
        with self.assertRaises(RuntimeTargetHostQualificationError):
            RuntimeTargetHostProvenance.parse(
                raw_by_id[_KIND_IDS[CAMPAIGN_EVIDENCE_KIND]] + b"\n"
            )

    def test_terminal_profile_cross_binds_artifacts_release_and_retained_payloads(self):
        raw_by_id, evidence_refs, _binding = material()
        result = self._verify(raw_by_id, evidence_refs)
        self.assertEqual(result.source_sha, SOURCE)
        self.assertEqual(result.host_fingerprint, HOST)
        self.assertEqual(result.spec_digest, SPEC)
        self.assertEqual(result.release_artifact_id, RELEASE_ID)
        self.assertEqual(result.release_artifact_sha256, RELEASE_SHA)
        self.assertEqual(
            result.payload_artifact_id_by_kind[RESOURCE_EVIDENCE_KIND],
            _PAYLOAD_IDS[RESOURCE_EVIDENCE_KIND],
        )
        self.assertEqual(
            result.payload_sha256_by_kind[RESOURCE_EVIDENCE_KIND],
            _PAYLOAD_DIGEST[RESOURCE_EVIDENCE_KIND],
        )
        self.assertEqual(
            result.collector_by_kind[CAMPAIGN_EVIDENCE_KIND],
            f"{CAMPAIGN_COLLECTOR_ID}@{CAMPAIGN_COLLECTOR_VERSION}",
        )
        self.assertEqual(
            result.collector_by_kind[HOST_INVENTORY_EVIDENCE_KIND],
            f"{HOST_INVENTORY_COLLECTOR_ID}@{HOST_INVENTORY_COLLECTOR_VERSION}",
        )

    def test_signed_binding_cannot_hide_resource_artifact_from_another_host(self):
        raw_by_id, evidence_refs, _binding = material(
            provenance_overrides={
                RESOURCE_EVIDENCE_KIND: {
                    "host_fingerprint": "sha256:" + "9" * 64,
                }
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "provenance identity conflicts.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_binding_digest_substitution_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material(
            binding_overrides={"resource_evidence_sha256": "sha256:" + "9" * 64}
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "does not match.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_missing_staleness_family_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material()
        evidence_refs = tuple(
            ref_value for ref_value in evidence_refs
            if ref_value.evidence_kind != STALENESS_EVIDENCE_KIND
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "evidence set mismatch",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_release_identity_is_required_before_canonical_trust_dispatch(self):
        raw_by_id, evidence_refs, _binding = material()
        signed = receipt(evidence_refs)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
            ) as canonical:
                with self.assertRaises(RuntimeTargetHostQualificationError):
                    verify_runtime_target_host_qualification(
                        signed,
                        evidence_store=store,
                        evidence_root=directory,
                        expected_source_sha=SOURCE,
                        expected_scenario_id="target-host-primary",
                        expected_spec_digest=SPEC,
                        expected_configuration_hash=CONFIG,
                        expected_host_fingerprint=HOST,
                        expected_workload_profile_hash=WORKLOAD,
                        expected_journal_store_identity_digest=JOURNAL,
                        expected_release_artifact_id=None,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )
            canonical.assert_not_called()

    def test_canonical_attestation_release_mismatch_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material()
        mismatched = accepted(
            evidence_refs,
            release_artifact_id=ALT_RELEASE_ID,
            release_artifact_sha256=ALT_RELEASE_SHA,
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "different release artifact",
        ):
            self._verify(raw_by_id, evidence_refs, accepted_value=mismatched)

    def test_binding_release_identity_mismatch_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material(
            binding_overrides={"release_artifact_id": ALT_RELEASE_ID}
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "binding identity does not match",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_provenance_release_identity_mismatch_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material(
            provenance_overrides={
                RESOURCE_EVIDENCE_KIND: {
                    "release_artifact_sha256": ALT_RELEASE_SHA,
                }
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "provenance identity conflicts.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_missing_retained_raw_payload_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material()
        del raw_by_id[_PAYLOAD_IDS[RESOURCE_EVIDENCE_KIND]]
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "retained raw payload is unavailable.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_tampered_retained_raw_payload_is_rejected(self):
        raw_by_id, evidence_refs, _binding = material()
        raw_by_id[_PAYLOAD_IDS[RESOURCE_EVIDENCE_KIND]] = b"tampered-resource-payload"
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "retained raw payload digest mismatch.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_retained_raw_payload_cannot_alias_signed_envelope(self):
        raw_by_id, evidence_refs, _binding = material(
            provenance_overrides={
                RESOURCE_EVIDENCE_KIND: {
                    "payload_artifact_id": _KIND_IDS[BINDING_EVIDENCE_KIND],
                }
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "raw payload artifact is not independent.*RESOURCES",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_campaign_payload_from_another_configuration_is_rejected(self):
        other_campaign = _campaign_raw(
            configuration_hash="sha256:" + ("9" * 64),
        )
        raw_by_id, evidence_refs, _binding = material(
            raw_payload_overrides={
                CAMPAIGN_EVIDENCE_KIND: other_campaign,
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "campaign observation identity conflicts",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_campaign_collector_must_match_raw_payload(self):
        raw_by_id, evidence_refs, _binding = material(
            provenance_overrides={
                CAMPAIGN_EVIDENCE_KIND: {
                    "collector_id": "noncanonical-campaign-collector",
                }
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "campaign provenance collector conflicts",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_host_inventory_payload_from_another_host_is_rejected(self):
        other_identity = {**HOST_IDENTITY, "cpu_count": 16}
        other_inventory = RuntimeTargetHostInventory(
            host_identity=other_identity,
            host_fingerprint=host_identity_fingerprint(other_identity),
        ).canonical_bytes()
        raw_by_id, evidence_refs, _binding = material(
            raw_payload_overrides={
                HOST_INVENTORY_EVIDENCE_KIND: other_inventory,
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "inventory belongs to another host",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_host_inventory_collector_must_match_raw_payload(self):
        raw_by_id, evidence_refs, _binding = material(
            provenance_overrides={
                HOST_INVENTORY_EVIDENCE_KIND: {
                    "collector_id": "noncanonical-host-inventory-collector",
                }
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "inventory provenance collector conflicts",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_host_inventory_raw_bytes_must_be_canonical(self):
        noncanonical = _RAW_PAYLOAD[HOST_INVENTORY_EVIDENCE_KIND] + b"\n"
        raw_by_id, evidence_refs, _binding = material(
            raw_payload_overrides={
                HOST_INVENTORY_EVIDENCE_KIND: noncanonical,
            }
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "inventory payload is not canonical",
        ):
            self._verify(raw_by_id, evidence_refs)

    def test_signed_profile_parsers_enforce_bounded_json_domain(self):
        parsers = (
            RuntimeTargetHostBinding.parse,
            RuntimeTargetHostProvenance.parse,
        )
        adversarial = (
            b"[" + (b" " * 1_000_001) + b"]",
            (b"[" * 129) + b"0" + (b"]" * 129),
            b'{"x":' + (b"9" * 641) + b"}",
        )
        for parser in parsers:
            for raw in adversarial:
                with self.subTest(parser=parser.__qualname__, size=len(raw)):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostQualificationError,
                        "bounded JSON domain",
                    ):
                        parser(raw)

    def test_canonical_trust_failure_happens_before_any_profile_read(self):
        raw_by_id, evidence_refs, _binding = material()
        signed = receipt(evidence_refs)
        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                side_effect=QualificationTrustError("scope unavailable"),
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
            ) as reader:
                with self.assertRaisesRegex(QualificationTrustError, "scope unavailable"):
                    verify_runtime_target_host_qualification(
                        signed,
                        evidence_store=store,
                        evidence_root=directory,
                        expected_source_sha=SOURCE,
                        expected_scenario_id="target-host-primary",
                        expected_spec_digest=SPEC,
                        expected_configuration_hash=CONFIG,
                        expected_host_fingerprint=HOST,
                        expected_workload_profile_hash=WORKLOAD,
                        expected_journal_store_identity_digest=JOURNAL,
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )
            reader.assert_not_called()

    def test_nonpass_cannot_be_promoted_to_terminal_target_host_qualification(self):
        raw_by_id, evidence_refs, _binding = material()
        inconclusive = accepted(
            evidence_refs,
            result="INCONCLUSIVE",
            unresolved_limits=("pressure-run-not-complete",),
        )
        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "requires signed PASS",
        ):
            self._verify(raw_by_id, evidence_refs, accepted_value=inconclusive)


if __name__ == "__main__":
    unittest.main()
