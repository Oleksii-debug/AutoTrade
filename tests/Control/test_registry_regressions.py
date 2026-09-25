from copy import deepcopy
import unittest

from control.tools.branch_lease_guard import evaluate_guard
from control.tools.registry_state import RegistryProtocolError, RegistryCollisionError
from test_registry_state import claim, renew, release, empty_registry, request, NOW


class RegistryRegressionTests(unittest.TestCase):
    def test_parent_child_overlap_is_independent_of_labels_and_case(self):
        state, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        for scope in ("contracts/jsonschema/order.json", "Contracts", "contracts/jsonschema/"):
            with self.subTest(scope=scope):
                other = request("second", "second", [scope])
                other.update(authority_family="OTHER", semantic_key="other")
                with self.assertRaises(RegistryCollisionError):
                    claim(state, other, expected_generation=1, now=NOW)

    def test_similar_directory_names_are_disjoint(self):
        state, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        other = request("second", "second", ["contracts/jsonschema-other"])
        state, _ = claim(state, other, expected_generation=1, now=NOW)
        self.assertEqual(len(state["claims"]), 2)

    def test_multi_scope_claim_is_all_or_none(self):
        state, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        original = deepcopy(state)
        with self.assertRaises(RegistryCollisionError):
            claim(state, request("second", "second", ["free", "contracts"]), expected_generation=1, now=NOW)
        self.assertEqual(state, original)

    def test_malformed_active_lease_does_not_release_ownership(self):
        state, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        state["claims"][0]["lease_until"] = "invalid"
        with self.assertRaises(RegistryProtocolError):
            claim(state, request("second", "second"), expected_generation=1, now=NOW)

    def test_lost_claim_reply_replays_after_generation_change_and_renew(self):
        state, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        state, _ = renew(
            state,
            claim_id=created["claim_id"],
            run_id="run-a",
            expected_generation=1,
            now="2026-09-22T10:30:00Z",
            lease_ttl_seconds=3600,
        )
        replay, result = claim(
            state,
            request(),
            expected_generation=0,
            now="2026-09-22T11:01:00Z",
        )
        self.assertEqual(replay, state)
        self.assertEqual(result["claim_id"], created["claim_id"])
        self.assertEqual(result["lease_until"], "2026-09-22T11:30:00Z")

    def test_expired_claim_cannot_release(self):
        state, created = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        with self.assertRaises(RegistryProtocolError):
            release(state, claim_id=created["claim_id"], run_id="run-a", expected_generation=1, now="2026-09-22T12:00:00Z", reason="late")

    def test_guard_requires_coverage_of_all_changed_paths(self):
        state, _ = claim(empty_registry(), request(), expected_generation=0, now=NOW)
        mutation = dict(authority_family="CONTRACT", semantic_key="core-schemas", mutation_scope=["contracts/jsonschema", "src/unowned"], branch="wp/a", prior_head="a", current_head="b", mutations=[dict(head="b", run_id="run-a", parent_heads=["a"])])
        self.assertEqual(evaluate_guard(state, mutation, now=NOW)["status"], "AMBIGUOUS")
