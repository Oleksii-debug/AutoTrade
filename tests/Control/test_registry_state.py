from __future__ import annotations

from copy import deepcopy
import unittest

from control.tools.branch_lease_guard import evaluate_guard
from control.tools.registry_state import (
    RegistryCollisionError,
    RegistryProtocolError,
    RegistryStaleGenerationError,
    active_mutation_claims,
    authorize_integration as registry_authorize_integration,
    claim as registry_claim,
    complete_integration as registry_complete_integration,
    expire_leases,
    release as registry_release,
    renew as registry_renew,
    submit_review as registry_submit_review,
)


NOW = "2026-09-22T10:00:00Z"


SERVICE_IDENTITY = {
    "service_id": "autotrade-claim-service",
    "principal_id": "github-app-installation:test",
    "authorized_account_id": "worker-account-a",
    "authentication_binding_digest": "sha256:" + ("a" * 64),
    "allowed_claim_modes": [
        "SOURCE_MUTATION",
        "INTEGRATION",
        "READ_ONLY_AUDIT",
        "RESEARCH",
        "CI_TRIAGE",
    ],
}

INTEGRATION_IDENTITY = {
    "service_id": "autotrade-integration-service",
    "principal_id": "github-app-installation:integration-test",
    "authorized_account_id": "worker-account-a",
    "authentication_binding_digest": "sha256:" + ("d" * 64),
    "allowed_claim_modes": ["INTEGRATION"],
}


ADMISSION_EVIDENCE = {
    "work_package_id": "WP-04",
    "bank_revision": "b" * 40,
    "readiness": "READY",
    "dependencies_satisfied": True,
    "blocking_findings_clear": True,
    "evidence_digest": "sha256:" + ("c" * 64),
}


def claim(*args, **kwargs):
    kwargs.setdefault("service_identity", SERVICE_IDENTITY)
    request_value = args[1] if len(args) > 1 else kwargs.get("request")
    if (
        isinstance(request_value, dict)
        and request_value.get("claim_mode") in {"SOURCE_MUTATION", "INTEGRATION"}
    ):
        kwargs.setdefault("admission_evidence", ADMISSION_EVIDENCE)
    return registry_claim(*args, **kwargs)


def renew(*args, **kwargs):
    kwargs.setdefault("service_identity", SERVICE_IDENTITY)
    return registry_renew(*args, **kwargs)


def release(*args, **kwargs):
    kwargs.setdefault("service_identity", SERVICE_IDENTITY)
    return registry_release(*args, **kwargs)


def empty_registry(mode="ATOMIC_CLAIMS_ENABLED"):
    return {
        "schema_version": "1.0.0",
        "generation": 0,
        "mode": mode,
        "updated_at": NOW,
        "claims": [],
    }


def request(request_id="req-a", run_id="run-a", scope=None, mode="SOURCE_MUTATION"):
    return {
        "request_id": request_id,
        "run_id": run_id,
        "account_id": "worker-account-a",
        "claim_mode": mode,
        "authority_family": "CONTRACT",
        "semantic_key": "core-schemas",
        "mutation_scope": scope or ["contracts/jsonschema"],
        "base_head": "base-a",
        "contract_versions": {"contracts": "1.0.0"},
    }


class RegistryTests(unittest.TestCase):
    def test_claim_increments_generation(self):
        registry, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        self.assertEqual(registry["generation"], 1)
        self.assertEqual(created["status"], "ACTIVE")

    def test_mutating_claim_requires_explicit_service_identity(self):
        with self.assertRaisesRegex(RegistryProtocolError, "service_identity"):
            registry_claim(
                empty_registry(),
                request(),
                expected_generation=0,
                now=NOW,
            )

    def test_mutating_claim_requires_admission_evidence(self):
        with self.assertRaisesRegex(RegistryProtocolError, "admission_evidence"):
            registry_claim(
                empty_registry(),
                request(),
                expected_generation=0,
                now=NOW,
                service_identity=SERVICE_IDENTITY,
            )

    def test_service_identity_is_account_and_mode_bound(self):
        wrong_account = dict(SERVICE_IDENTITY)
        wrong_account["authorized_account_id"] = "worker-account-b"
        with self.assertRaisesRegex(RegistryProtocolError, "request account"):
            registry_claim(
                empty_registry(),
                request(),
                expected_generation=0,
                now=NOW,
                service_identity=wrong_account,
            )

        read_only_identity = dict(SERVICE_IDENTITY)
        read_only_identity["allowed_claim_modes"] = ["READ_ONLY_AUDIT"]
        with self.assertRaisesRegex(RegistryProtocolError, "claim mode"):
            registry_claim(
                empty_registry(),
                request(),
                expected_generation=0,
                now=NOW,
                service_identity=read_only_identity,
            )

    def test_request_id_replay_cannot_change_service_owner(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        other_owner = dict(SERVICE_IDENTITY)
        other_owner["authentication_binding_digest"] = "sha256:" + ("b" * 64)
        with self.assertRaisesRegex(RegistryProtocolError, "different service identity"):
            registry_claim(
                first,
                request(),
                expected_generation=1,
                now=NOW,
                service_identity=other_owner,
                admission_evidence=ADMISSION_EVIDENCE,
            )

    def test_claim_lease_is_issued_from_service_time_and_bounded_policy(self):
        registry, created = claim(
            empty_registry(),
            request(),
            expected_generation=0,
            now=NOW,
            lease_ttl_seconds=900,
        )
        self.assertEqual(created["lease_until"], "2026-09-22T10:15:00Z")
        self.assertEqual(created["lease_ttl_seconds"], 900)
        self.assertEqual(registry["claims"][0]["lease_until"], created["lease_until"])

        with self.assertRaisesRegex(RegistryProtocolError, "lease_ttl_seconds"):
            claim(
                empty_registry(),
                request("req-too-long"),
                expected_generation=0,
                now=NOW,
                lease_ttl_seconds=3601,
            )

    def test_request_cannot_supply_lease_expiry(self):
        worker_request = request()
        worker_request["lease_until"] = "2099-01-01T00:00:00Z"
        with self.assertRaisesRegex(RegistryProtocolError, "service-issued"):
            claim(
                empty_registry(),
                worker_request,
                expected_generation=0,
                now=NOW,
            )

    def test_disabled_registry_rejects_mutating_claims_but_allows_read_only(self):
        for mode in ("BOOTSTRAP_NOT_ENABLED", "PROTOCOL_IMPLEMENTED_NOT_ENABLED"):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                RegistryProtocolError, "mutation claims are disabled"
            ):
                claim(
                    empty_registry(mode),
                    request(),
                    expected_generation=0,
                    now=NOW,
                )
            read_only, created = claim(
                empty_registry(mode),
                request(mode="READ_ONLY_AUDIT"),
                expected_generation=0,
                now=NOW,
            )
            self.assertEqual(read_only["generation"], 1)
            self.assertEqual(created["claim_mode"], "READ_ONLY_AUDIT")

    def test_unknown_registry_mode_fails_closed(self):
        with self.assertRaisesRegex(RegistryProtocolError, "registry mode"):
            claim(
                empty_registry("MAYBE_ENABLED"),
                request(mode="READ_ONLY_AUDIT"),
                expected_generation=0,
                now=NOW,
            )

    def test_same_request_is_idempotent(self):
        first, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        second, replayed = claim(first, request(), expected_generation=1, now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(created["claim_id"], replayed["claim_id"])

    def test_request_id_payload_change_fails(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        changed = request()
        changed["run_id"] = "other"
        with self.assertRaises(RegistryProtocolError):
            claim(first, changed, expected_generation=1, now=NOW)

    def test_overlapping_mutation_collides(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        with self.assertRaises(RegistryCollisionError):
            claim(first, request("req-b", "run-b"), expected_generation=1, now=NOW)

    def test_read_only_does_not_take_mutation_exclusivity(self):
        first, _ = claim(empty_registry(), request(mode="READ_ONLY_AUDIT"), expected_generation=0, now=NOW)
        second, created = claim(first, request("req-b", "run-b"), expected_generation=1, now=NOW)
        self.assertEqual(second["generation"], 2)
        self.assertEqual(created["run_id"], "run-b")

    def test_disjoint_paths_can_run_in_parallel(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        other = request("req-b", "run-b", scope=["src/AutoTrade.Providers.Bybit"])
        other["semantic_key"] = "provider-bybit"
        second, _ = claim(first, other, expected_generation=1, now=NOW)
        self.assertEqual(len(active_mutation_claims(second, now=NOW)), 2)

    def test_stale_generation_cannot_claim(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        other = request("req-b", "run-b")
        other["semantic_key"] = "other"
        with self.assertRaises(RegistryStaleGenerationError):
            claim(first, other, expected_generation=0, now=NOW)

    def test_renew_requires_generation_and_extends_only(self):
        first, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        renewed, value = renew(
            first,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=1,
            now="2026-09-22T10:30:00Z",
            lease_ttl_seconds=3600,
        )
        self.assertEqual(renewed["generation"], 2)
        self.assertEqual(value["lease_until"], "2026-09-22T11:30:00Z")
        self.assertEqual(value["lease_ttl_seconds"], 3600)
        with self.assertRaises(RegistryStaleGenerationError):
            renew(
                renewed,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=1,
                now="2026-09-22T10:45:00Z",
            )

    def test_renewal_ttl_is_service_bounded(self):
        first, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        with self.assertRaisesRegex(RegistryProtocolError, "lease_ttl_seconds"):
            renew(
                first,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=1,
                now="2026-09-22T10:30:00Z",
                lease_ttl_seconds=7200,
            )
        self.assertEqual(first["generation"], 1)
        self.assertEqual(first["claims"][0]["lease_until"], "2026-09-22T11:00:00Z")

    def test_release_terminates_owner(self):
        first, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        released, value = release(
            first,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=1,
            now=NOW,
            reason="done",
        )
        self.assertEqual(value["status"], "RELEASED")
        self.assertEqual(active_mutation_claims(released, now=NOW), [])

    def test_foreign_service_cannot_renew_or_release_claim(self):
        first, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        foreign = dict(SERVICE_IDENTITY)
        foreign["principal_id"] = "github-app-installation:other"
        with self.assertRaisesRegex(RegistryProtocolError, "service identity does not own claim"):
            registry_renew(
                first,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=1,
                now="2026-09-22T10:30:00Z",
                service_identity=foreign,
            )
        with self.assertRaisesRegex(RegistryProtocolError, "service identity does not own claim"):
            registry_release(
                first,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=1,
                now=NOW,
                reason="foreign",
                service_identity=foreign,
            )
        self.assertEqual(first["generation"], 1)
        self.assertEqual(first["claims"][0]["status"], "ACTIVE")

    def test_review_integration_completion_fences_exact_pr_head_and_claim_generation(self):
        first, created = claim(
            empty_registry(),
            request(),
            expected_generation=0,
            now=NOW,
        )
        head_sha = "1" * 40
        reviewed, review = registry_submit_review(
            first,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=1,
            expected_claim_generation=1,
            now="2026-09-22T10:01:00Z",
            service_identity=SERVICE_IDENTITY,
            pr_number=101,
            head_sha=head_sha,
            review_evidence_digest="sha256:" + ("2" * 64),
        )
        self.assertEqual(reviewed["generation"], 2)
        self.assertEqual(
            reviewed["claims"][0]["handoff_state"],
            "REVIEW_SUBMITTED",
        )
        self.assertEqual(review["fenced_claim_generation"], 1)

        authorized, authorization = registry_authorize_integration(
            reviewed,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=2,
            expected_claim_generation=2,
            now="2026-09-22T10:02:00Z",
            service_identity=INTEGRATION_IDENTITY,
            pr_number=101,
            head_sha=head_sha,
            required_checks_digest="sha256:" + ("3" * 64),
            integration_evidence_digest="sha256:" + ("4" * 64),
        )
        self.assertEqual(authorized["generation"], 3)
        self.assertEqual(
            authorized["claims"][0]["handoff_state"],
            "INTEGRATION_AUTHORIZED",
        )
        self.assertEqual(authorization["head_sha"], head_sha)

        completed, completion = registry_complete_integration(
            authorized,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=3,
            expected_claim_generation=3,
            now="2026-09-22T10:03:00Z",
            service_identity=INTEGRATION_IDENTITY,
            pr_number=101,
            head_sha=head_sha,
            merge_commit_sha="5" * 40,
        )
        self.assertEqual(completed["generation"], 4)
        self.assertEqual(completed["claims"][0]["status"], "COMPLETED")
        self.assertEqual(completed["claims"][0]["handoff_state"], "COMPLETED")
        self.assertEqual(completion["merge_commit_sha"], "5" * 40)
        self.assertEqual(active_mutation_claims(completed, now=NOW), [])

    def test_review_submission_requires_current_claim_owner_and_claim_generation(self):
        first, created = claim(
            empty_registry(),
            request(),
            expected_generation=0,
            now=NOW,
        )
        foreign = dict(SERVICE_IDENTITY)
        foreign["principal_id"] = "github-app-installation:foreign"
        with self.assertRaisesRegex(
            RegistryProtocolError,
            "does not own claim review submission",
        ):
            registry_submit_review(
                first,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=1,
                expected_claim_generation=1,
                now="2026-09-22T10:01:00Z",
                service_identity=foreign,
                pr_number=102,
                head_sha="6" * 40,
                review_evidence_digest="sha256:" + ("7" * 64),
            )

        with self.assertRaisesRegex(
            RegistryStaleGenerationError,
            "stale claim generation",
        ):
            registry_submit_review(
                first,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=1,
                expected_claim_generation=999,
                now="2026-09-22T10:01:00Z",
                service_identity=SERVICE_IDENTITY,
                pr_number=102,
                head_sha="6" * 40,
                review_evidence_digest="sha256:" + ("7" * 64),
            )
        self.assertEqual(first["claims"][0]["handoff_state"], "CLAIMED")

    def test_unrelated_registry_generation_does_not_replace_per_claim_fence(self):
        first, created = claim(
            empty_registry(),
            request(),
            expected_generation=0,
            now=NOW,
        )
        other = request(
            "req-b",
            "run-b",
            scope=["src/AutoTrade.Providers.Bybit"],
        )
        other["semantic_key"] = "provider-bybit"
        second, _ = claim(
            first,
            other,
            expected_generation=1,
            now=NOW,
        )
        self.assertEqual(second["generation"], 2)
        self.assertEqual(second["claims"][0]["claim_generation"], 1)

        reviewed, _ = registry_submit_review(
            second,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=2,
            expected_claim_generation=1,
            now="2026-09-22T10:01:00Z",
            service_identity=SERVICE_IDENTITY,
            pr_number=103,
            head_sha="8" * 40,
            review_evidence_digest="sha256:" + ("9" * 64),
        )
        self.assertEqual(reviewed["claims"][0]["claim_generation"], 3)

        with self.assertRaisesRegex(
            RegistryStaleGenerationError,
            "stale claim generation",
        ):
            registry_authorize_integration(
                reviewed,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=3,
                expected_claim_generation=1,
                now="2026-09-22T10:02:00Z",
                service_identity=INTEGRATION_IDENTITY,
                pr_number=103,
                head_sha="8" * 40,
                required_checks_digest="sha256:" + ("a" * 64),
                integration_evidence_digest="sha256:" + ("b" * 64),
            )

    def test_restored_registry_rejects_hidden_future_handoff_evidence(self):
        first, created = claim(
            empty_registry(),
            request(),
            expected_generation=0,
            now=NOW,
        )
        reviewed, _ = registry_submit_review(
            first,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=1,
            expected_claim_generation=1,
            now="2026-09-22T10:01:00Z",
            service_identity=SERVICE_IDENTITY,
            pr_number=107,
            head_sha="7" * 40,
            review_evidence_digest="sha256:" + ("8" * 64),
        )
        tampered = deepcopy(reviewed)
        tampered["claims"][0]["integration_authorization"] = {
            "pr_number": 107,
            "head_sha": "7" * 40,
            "required_checks_digest": "sha256:" + ("9" * 64),
            "integration_evidence_digest": "sha256:" + ("a" * 64),
            "authorized_at": "2026-09-22T10:02:00Z",
            "fenced_claim_generation": 2,
            "service_identity": deepcopy(INTEGRATION_IDENTITY),
        }
        with self.assertRaisesRegex(
            RegistryProtocolError,
            "cannot contain integration authorization",
        ):
            active_mutation_claims(tampered, now=NOW)

    def test_integration_cannot_retarget_submitted_pr_or_head(self):
        first, created = claim(
            empty_registry(),
            request(),
            expected_generation=0,
            now=NOW,
        )
        reviewed, _ = registry_submit_review(
            first,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=1,
            expected_claim_generation=1,
            now="2026-09-22T10:01:00Z",
            service_identity=SERVICE_IDENTITY,
            pr_number=104,
            head_sha="c" * 40,
            review_evidence_digest="sha256:" + ("d" * 64),
        )
        for pr_number, head_sha in (
            (105, "c" * 40),
            (104, "e" * 40),
        ):
            with self.subTest(
                pr_number=pr_number,
                head_sha=head_sha,
            ), self.assertRaisesRegex(
                RegistryProtocolError,
                "retargets reviewed PR/head",
            ):
                registry_authorize_integration(
                    reviewed,
                    claim_id=created["claim_id"],
                    run_id="run-a",
                    expected_generation=2,
                    expected_claim_generation=2,
                    now="2026-09-22T10:02:00Z",
                    service_identity=INTEGRATION_IDENTITY,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    required_checks_digest="sha256:" + ("f" * 64),
                    integration_evidence_digest="sha256:" + ("0" * 64),
                )
        self.assertEqual(
            reviewed["claims"][0]["handoff_state"],
            "REVIEW_SUBMITTED",
        )

    def test_completion_requires_same_integrator_and_exact_authorized_head(self):
        first, created = claim(
            empty_registry(),
            request(),
            expected_generation=0,
            now=NOW,
        )
        reviewed, _ = registry_submit_review(
            first,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=1,
            expected_claim_generation=1,
            now="2026-09-22T10:01:00Z",
            service_identity=SERVICE_IDENTITY,
            pr_number=106,
            head_sha="1" * 40,
            review_evidence_digest="sha256:" + ("2" * 64),
        )
        authorized, _ = registry_authorize_integration(
            reviewed,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=2,
            expected_claim_generation=2,
            now="2026-09-22T10:02:00Z",
            service_identity=INTEGRATION_IDENTITY,
            pr_number=106,
            head_sha="1" * 40,
            required_checks_digest="sha256:" + ("3" * 64),
            integration_evidence_digest="sha256:" + ("4" * 64),
        )

        other_integrator = dict(INTEGRATION_IDENTITY)
        other_integrator["principal_id"] = "github-app-installation:other-integrator"
        with self.assertRaisesRegex(
            RegistryProtocolError,
            "differs from integration authorization",
        ):
            registry_complete_integration(
                authorized,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=3,
                expected_claim_generation=3,
                now="2026-09-22T10:03:00Z",
                service_identity=other_integrator,
                pr_number=106,
                head_sha="1" * 40,
                merge_commit_sha="5" * 40,
            )

        with self.assertRaisesRegex(
            RegistryProtocolError,
            "retargets authorized PR/head",
        ):
            registry_complete_integration(
                authorized,
                claim_id=created["claim_id"],
                run_id="run-a",
                expected_generation=3,
                expected_claim_generation=3,
                now="2026-09-22T10:03:00Z",
                service_identity=INTEGRATION_IDENTITY,
                pr_number=106,
                head_sha="6" * 40,
                merge_commit_sha="5" * 40,
            )

    def test_expiry_closes_stale_lease(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        expired = expire_leases(first, now="2026-09-22T11:00:01Z")
        self.assertEqual(expired["claims"][0]["status"], "EXPIRED")
        self.assertEqual(expired["generation"], 2)

    def test_branch_guard_rejects_foreign_writer(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        mutation = {
            "authority_family": "CONTRACT",
            "semantic_key": "core-schemas",
            "mutation_scope": ["contracts/jsonschema"],
            "branch": "worker/run-a",
            "prior_head": "old",
            "current_head": "new",
            "mutations": [{"head": "new", "run_id": "run-b", "parent_heads": ["old"]}],
        }
        result = evaluate_guard(first, mutation, now=NOW)
        self.assertEqual(result["status"], "COLLISION")

    def test_branch_guard_accepts_owner_and_merge_parent(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        mutation = {
            "authority_family": "CONTRACT",
            "semantic_key": "core-schemas",
            "mutation_scope": ["contracts/jsonschema"],
            "branch": "worker/run-a",
            "prior_head": "old",
            "current_head": "merge",
            "mutations": [{"head": "merge", "run_id": "run-a", "parent_heads": ["old", "main-new"]}],
        }
        result = evaluate_guard(first, mutation, now=NOW)
        self.assertEqual(result["status"], "OK")

    def test_branch_guard_fails_closed_on_missing_parent_evidence(self):
        first, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        mutation = {
            "authority_family": "CONTRACT",
            "semantic_key": "core-schemas",
            "mutation_scope": ["contracts/jsonschema"],
            "branch": "worker/run-a",
            "prior_head": "old",
            "current_head": "new",
            "mutations": [{"head": "new", "run_id": "run-a"}],
        }
        result = evaluate_guard(first, mutation, now=NOW)
        self.assertEqual(result["status"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
