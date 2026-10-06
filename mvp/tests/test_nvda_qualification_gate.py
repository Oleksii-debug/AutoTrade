                        artifact_id="33333333-3333-4333-8333-333333333333",
                        sha256=evidence_sha,
                        evidence_kind="NVDA_REAL_RUN",
                        source_sha=evidence["source_sha"],
                    ),
                ),
            )
        )
        accepted = SimpleNamespace(
            result="PASS",
            attestation_id="22222222-2222-4222-8222-222222222222",
            attestation_digest="sha256:" + "1" * 64,
            policy_id="sha256:" + "2" * 64,
            trust_root_id="sha256:" + "3" * 64,
            requirement_ids=requirement_ids,
            release_artifact_id=receipt.attestation.release_artifact_id,
            release_artifact_sha256=release_sha,
            evidence_refs=receipt.attestation.evidence_refs,
        )
        with patch(
            "tools.check_nvda_qualification.verify_qualification_attestation",
            return_value=accepted,
        ) as verify:
            result = validate_trusted_nvda_qualification(
                evidence,
                REQUIREMENTS,
                evidence_sha256=evidence_sha,
                release_artifact_sha256=release_sha,
                receipt=receipt,
                policy=SimpleNamespace(),
                evidence_store=SimpleNamespace(),
                evidence_root=ROOT,
                expected_policy_id="sha256:" + "4" * 64,
                expected_policy_version="2026.09",
            )
        self.assertTrue(result["qualified"])
        self.assertEqual(result["attestation_id"], accepted.attestation_id)
        self.assertEqual(verify.call_count, len(requirement_ids))

        bad_receipt = receipt
        mismatched_accepted = SimpleNamespace(
            result="PASS",
            attestation_id=accepted.attestation_id,
            attestation_digest=accepted.attestation_digest,
            policy_id=accepted.policy_id,
            trust_root_id=accepted.trust_root_id,
            requirement_ids=requirement_ids[:-1],
            release_artifact_id=receipt.attestation.release_artifact_id,
            release_artifact_sha256=release_sha,
            evidence_refs=receipt.attestation.evidence_refs,
        )
        with (
            patch(
                "tools.check_nvda_qualification.verify_qualification_attestation",
                return_value=mismatched_accepted,
            ),
            self.assertRaisesRegex(NvdaQualificationError, "workflow set"),
        ):
            validate_trusted_nvda_qualification(
                evidence,
                REQUIREMENTS,
                evidence_sha256=evidence_sha,
                release_artifact_sha256=release_sha,
                receipt=bad_receipt,
                policy=SimpleNamespace(),
                evidence_store=SimpleNamespace(),
                evidence_root=ROOT,
                expected_policy_id="sha256:" + "4" * 64,
                expected_policy_version="2026.09",
            )

        mismatched_release = SimpleNamespace(
            result="PASS",
            attestation_id=accepted.attestation_id,
            attestation_digest=accepted.attestation_digest,
            policy_id=accepted.policy_id,
            trust_root_id=accepted.trust_root_id,
            requirement_ids=requirement_ids,
            release_artifact_id="22222222-2222-4222-8222-222222222222",
            release_artifact_sha256=release_sha,
            evidence_refs=receipt.attestation.evidence_refs,
        )
        with (
            patch(
                "tools.check_nvda_qualification.verify_qualification_attestation",
                return_value=mismatched_release,
            ),
            self.assertRaisesRegex(NvdaQualificationError, "release identity"),
        ):
            validate_trusted_nvda_qualification(
                evidence,
                REQUIREMENTS,
                evidence_sha256=evidence_sha,
                release_artifact_sha256=release_sha,
                receipt=receipt,