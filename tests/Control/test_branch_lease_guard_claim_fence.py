from copy import deepcopy
import unittest

from control.tools.branch_lease_guard import evaluate_guard

NOW = "2026-09-22T10:00:00Z"
CLAIM_ID = "11111111-1111-1111-1111-111111111111"
SERVICE = {
    "service_id": "autotrade-claim-service",
    "principal_id": "github-app-installation:test",
    "authorized_account_id": "worker-account-a",
    "authentication_binding_digest": "sha256:" + ("a" * 64),
    "allowed_claim_modes": ["SOURCE_MUTATION", "INTEGRATION", "READ_ONLY_AUDIT", "RESEARCH", "CI_TRIAGE"],
}
ADMISSION = {
    "work_package_id": "WP-04",
    "bank_revision": "b" * 40,
    "readiness": "READY",
    "dependencies_satisfied": True,
    "blocking_findings_clear": True,
    "evidence_digest": "sha256:" + ("c" * 64),
    "evaluated_at": NOW,
}


def _request():
    return {
        "request_id": "req-a",
        "run_id": "run-a",
        "account_id": "worker-account-a",
        "claim_mode": "SOURCE_MUTATION",
        "authority_family": "CONTRACT",
        "semantic_key": "core-schemas",
        "mutation_scope": ["contracts/jsonschema"],
        "base_head": "a" * 40,
        "contract_versions": {"contracts": "1.0.0"},
    }


def registry(*, mode="ATOMIC_CLAIMS_ENABLED", generation=7, claim_generation=7, handoff_state="CLAIMED"):
    req = _request()
    claim = {
        "claim_id": CLAIM_ID,
        **req,
        "original_request": deepcopy(req),
        "owner_identity": deepcopy(SERVICE),
        "admission_evidence": deepcopy(ADMISSION),
        "lease_until": "2026-09-22T11:00:00Z",
        "lease_ttl_seconds": 3600,
        "status": "ACTIVE",
        "handoff_state": handoff_state,
        "claimed_at": NOW,
        "claim_generation": claim_generation,
    }
    if handoff_state == "REVIEW_SUBMITTED":
        claim["review_submission"] = {
            "pr_number": 101,
            "head_sha": "d" * 40,
            "review_evidence_digest": "sha256:" + ("e" * 64),
            "submitted_at": "2026-09-22T10:01:00Z",
            "fenced_claim_generation": claim_generation - 1,
            "owner_identity": deepcopy(SERVICE),
        }
    return {
        "schema_version": "1.0.0",
        "generation": generation,
        "mode": mode,
        "updated_at": NOW,
        "claims": [claim],
    }


def mutation(*, claim_id=CLAIM_ID, claim_generation=7, moved=True):
    prior = "b" * 40
    current = "c" * 40 if moved else prior
    records = [] if not moved else [
        {
            "head": current,
            "run_id": "run-a",
            "claim_id": claim_id,
            "claim_generation": claim_generation,
            "parent_heads": [prior],
        }
    ]
    return {
        "authority_family": "CONTRACT",
        "semantic_key": "core-schemas",
        "mutation_scope": ["contracts/jsonschema"],
        "branch": "worker/run-a",
        "claim_id": claim_id,
        "claim_generation": claim_generation,
        "prior_head": prior,
        "current_head": current,
        "mutations": records,
    }


class BranchLeaseGuardClaimFenceTests(unittest.TestCase):
    def test_exact_live_claim_generation_allows_owned_movement(self):
        result = evaluate_guard(registry(), mutation(), now=NOW)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["admitted_claim_id"], CLAIM_ID)
        self.assertEqual(result["admitted_claim_generation"], 7)

    def test_disabled_registry_revokes_preexisting_active_claim(self):
        for mode in ("BOOTSTRAP_NOT_ENABLED", "PROTOCOL_IMPLEMENTED_NOT_ENABLED"):
            with self.subTest(mode=mode):
                result = evaluate_guard(registry(mode=mode), mutation(), now=NOW)
                self.assertEqual(result["status"], "NO_LIVE_OWNER")
                self.assertIn("disabled", result["evidence"][0])

    def test_stale_top_level_claim_generation_is_rejected(self):
        result = evaluate_guard(registry(claim_generation=8, generation=8), mutation(claim_generation=7), now=NOW)
        self.assertEqual(result["status"], "COLLISION")
        self.assertIn("claim_generation", result["evidence"][0])

    def test_wrong_claim_id_is_rejected_even_for_same_run(self):
        other = "22222222-2222-2222-2222-222222222222"
        result = evaluate_guard(registry(), mutation(claim_id=other), now=NOW)
        self.assertEqual(result["status"], "COLLISION")
        self.assertIn("claim_id", result["evidence"][0])

    def test_each_mutation_record_must_carry_current_claim_fence(self):
        value = mutation()
        value["mutations"][0].pop("claim_generation")
        result = evaluate_guard(registry(), value, now=NOW)
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIn("claim_generation", result["evidence"][0])

        value = mutation()
        value["mutations"][0]["claim_generation"] = 6
        result = evaluate_guard(registry(), value, now=NOW)
        self.assertEqual(result["status"], "COLLISION")
        self.assertIn("stale or foreign", result["evidence"][0])

    def test_branch_movement_is_frozen_after_review_submission(self):
        result = evaluate_guard(
            registry(generation=8, claim_generation=8, handoff_state="REVIEW_SUBMITTED"),
            mutation(claim_generation=8),
            now=NOW,
        )
        self.assertEqual(result["status"], "COLLISION")
        self.assertIn("frozen", result["evidence"][0])

    def test_no_movement_can_be_observed_after_review(self):
        result = evaluate_guard(
            registry(generation=8, claim_generation=8, handoff_state="REVIEW_SUBMITTED"),
            mutation(claim_generation=8, moved=False),
            now=NOW,
        )
        self.assertEqual(result["status"], "OK")

    def test_missing_top_level_fence_fails_closed(self):
        value = mutation()
        value.pop("claim_id")
        with self.assertRaisesRegex(ValueError, "claim_id"):
            evaluate_guard(registry(), value, now=NOW)

        value = mutation()
        value.pop("claim_generation")
        result = evaluate_guard(registry(), value, now=NOW)
        self.assertEqual(result["status"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
