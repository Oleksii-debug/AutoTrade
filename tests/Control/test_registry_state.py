from __future__ import annotations

import unittest

from control.tools.branch_lease_guard import evaluate_guard
from control.tools.registry_state import (
    RegistryCollisionError,
    RegistryProtocolError,
    RegistryStaleGenerationError,
    active_mutation_claims,
    claim,
    expire_leases,
    release,
    renew,
)


NOW = "2026-09-22T10:00:00Z"


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
