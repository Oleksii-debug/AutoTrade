"""Plan-9 Sections 9A-9C claimed-scope matrix contracts."""
from dataclasses import replace
import json
import unittest

from mvp.autotrade_mvp.evidence_class_matrix import (
    LIVE,
    NVDA_PHYSICAL,
    PAPER,
    REAL_PROVIDER,
    REPLAY,
    SIGNED_RELEASE,
    SIMULATION,
    SOURCE,
)
from mvp.autotrade_mvp.plan9_claimed_scope_matrix import (
    CLAIMED,
    NOT_ACTIVATED,
    NOT_CLAIMED,
    EvidencePointer,
    Plan9ClaimedScopeError,
    REQUIREMENTS,
    ScopeClaim,
    build_claimed_scope_matrix,
)


SHA = "a" * 40
OTHER_SHA = "b" * 40
PACKAGE = "sha256:" + "c" * 64
OTHER_PACKAGE = "sha256:" + "d" * 64
CONTRACT = "sha256:" + "e" * 64
EVIDENCE = "sha256:" + "f" * 64


def claims(*, claimed=frozenset()):
    values = []
    for capability in REQUIREMENTS:
        if capability in claimed:
            state = CLAIMED
        elif capability in {
            "target_host_performance", "provider_account", "paper_execution",
            "live_execution", "physical_nvda", "delivered_signed_release",
        }:
            state = NOT_ACTIVATED
        else:
            state = NOT_CLAIMED
        values.append(ScopeClaim(
            capability=capability,
            state=state,
            contract_digest=CONTRACT,
            limitation=f"{capability} remains bounded to its declared evidence class",
        ))
    return tuple(values)


def pointer(capability, evidence_class, *, source_sha=SHA,
            package_digest=PACKAGE, status="PASS", ref="artifact:1"):
    return EvidencePointer(
        capability=capability,
        evidence_class=evidence_class,
        source_sha=source_sha,
        package_digest=package_digest,
        evidence_digest=EVIDENCE,
        evidence_ref=ref,
        reported_status=status,
    )


def row(matrix, capability):
    return next(item for item in matrix.rows if item["capability"] == capability)


class Plan9ClaimedScopeMatrixTests(unittest.TestCase):
    def test_complete_provider_free_inventory_is_deterministic_and_non_authorizing(self):
        claimed = frozenset((
            "historical_replay", "zero_simulation", "accounting", "risk",
            "recovery", "security", "accessibility_source",
            "provenance_source", "economics_simulation",
        ))
        evidence = []
        for capability in claimed:
            for evidence_class in REQUIREMENTS[capability]:
                evidence.append(pointer(
                    capability,
                    evidence_class,
                    ref=f"artifact:{capability}:{evidence_class}",
                ))
        first = build_claimed_scope_matrix(
            source_sha=SHA, package_digest=PACKAGE,
            claims=claims(claimed=claimed), evidence=evidence,
        )
        second = build_claimed_scope_matrix(
            source_sha=SHA, package_digest=PACKAGE,
            claims=tuple(reversed(claims(claimed=claimed))),
            evidence=tuple(reversed(evidence)),
        )
        self.assertEqual(first.machine_readable(), second.machine_readable())
        result = first.machine_readable()
        self.assertEqual(result["schema"], "autotrade.plan9.claimed-scope-matrix.v1")
        self.assertEqual(result["trading_authority"], "NOT_AUTHORIZED")
        self.assertEqual(result["real_money"], "NOT_AUTHORIZED")
        self.assertEqual(result["final_release"], "NOT_AUTHORIZED")
        self.assertEqual(result["evidence_verification"], "EXTERNAL_REQUIRED")
        self.assertEqual(len(result["rows"]), len(REQUIREMENTS))
        json.dumps(result, allow_nan=False)

    def test_external_rows_are_explicit_not_activated(self):
        matrix = build_claimed_scope_matrix(
            source_sha=SHA, package_digest=PACKAGE,
            claims=claims(), evidence=(),
        )
        for capability in (
            "target_host_performance", "provider_account", "paper_execution",
            "live_execution", "physical_nvda", "delivered_signed_release",
        ):
            with self.subTest(capability=capability):
                item = row(matrix, capability)
                self.assertEqual(item["claim_state"], NOT_ACTIVATED)
                self.assertEqual(item["status"], "EXPLICITLY_NOT_ACTIVATED")
                self.assertFalse(item["independently_verified"])
                self.assertFalse(item["authorizes_trading"])

    def test_lower_classes_never_substitute_provider_paper_live_nvda_or_signing(self):
        cases = (
            ("provider_account", REAL_PROVIDER),
            ("paper_execution", PAPER),
            ("live_execution", LIVE),
            ("physical_nvda", NVDA_PHYSICAL),
            ("delivered_signed_release", SIGNED_RELEASE),
        )
        for capability, required in cases:
            with self.subTest(capability=capability):
                matrix = build_claimed_scope_matrix(
                    source_sha=SHA,
                    package_digest=PACKAGE,
                    claims=claims(claimed=frozenset((capability,))),
                    evidence=(
                        pointer(capability, SOURCE, ref="artifact:source"),
                        pointer(capability, SIMULATION, ref="artifact:simulation"),
                        pointer(capability, REPLAY, ref="artifact:replay"),
                    ),
                )
                item = row(matrix, capability)
                self.assertEqual(item["missing_evidence_classes"], [required])
                self.assertEqual(
                    set(item["rejected_wrong_classes"]),
                    {SOURCE, SIMULATION, REPLAY},
                )
                self.assertEqual(item["status"], "INCONCLUSIVE")

    def test_paper_does_not_satisfy_live(self):
        matrix = build_claimed_scope_matrix(
            source_sha=SHA,
            package_digest=PACKAGE,
            claims=claims(claimed=frozenset(("live_execution",))),
            evidence=(pointer("live_execution", PAPER),),
        )
        item = row(matrix, "live_execution")
        self.assertEqual(item["missing_evidence_classes"], [LIVE])
        self.assertEqual(item["rejected_wrong_classes"], [PAPER])

    def test_stale_or_other_package_evidence_is_not_current(self):
        matrix = build_claimed_scope_matrix(
            source_sha=SHA,
            package_digest=PACKAGE,
            claims=claims(claimed=frozenset(("historical_replay",))),
            evidence=(
                pointer("historical_replay", REPLAY, source_sha=OTHER_SHA,
                        ref="artifact:old-source"),
                pointer("historical_replay", REPLAY,
                        package_digest=OTHER_PACKAGE, ref="artifact:old-package"),
            ),
        )
        item = row(matrix, "historical_replay")
        self.assertEqual(item["missing_evidence_classes"], [REPLAY])
        self.assertEqual(item["stale_or_cross_package_evidence_count"], 2)
        self.assertEqual(item["reported_evidence_count"], 0)

    def test_reported_pass_stays_unverified_and_fail_stays_fail(self):
        passed = build_claimed_scope_matrix(
            source_sha=SHA, package_digest=PACKAGE,
            claims=claims(claimed=frozenset(("historical_replay",))),
            evidence=(pointer("historical_replay", REPLAY),),
        )
        self.assertEqual(row(passed, "historical_replay")["status"], "INCONCLUSIVE")
        self.assertFalse(row(passed, "historical_replay")["independently_verified"])

        failed = build_claimed_scope_matrix(
            source_sha=SHA, package_digest=PACKAGE,
            claims=claims(claimed=frozenset(("historical_replay",))),
            evidence=(pointer("historical_replay", REPLAY, status="FAIL"),),
        )
        self.assertEqual(row(failed, "historical_replay")["status"], "FAIL")

    def test_closed_world_claims_and_duplicate_evidence_fail(self):
        incomplete = claims()[:-1]
        with self.assertRaisesRegex(Plan9ClaimedScopeError, "closed capability set"):
            build_claimed_scope_matrix(
                source_sha=SHA, package_digest=PACKAGE,
                claims=incomplete, evidence=(),
            )
        duplicate_claim = claims() + (claims()[0],)
        with self.assertRaisesRegex(Plan9ClaimedScopeError, "duplicate capability"):
            build_claimed_scope_matrix(
                source_sha=SHA, package_digest=PACKAGE,
                claims=duplicate_claim, evidence=(),
            )
        evidence = pointer("historical_replay", REPLAY)
        with self.assertRaisesRegex(Plan9ClaimedScopeError, "duplicate evidence"):
            build_claimed_scope_matrix(
                source_sha=SHA, package_digest=PACKAGE,
                claims=claims(), evidence=(evidence, evidence),
            )

    def test_invalid_and_ambiguous_values_fail_closed(self):
        claim = claims()[0]
        evidence = pointer("historical_replay", REPLAY)
        for invalid in (
            lambda: replace(claim, state="PARKED"),
            lambda: replace(claim, contract_digest="sha256:bad"),
            lambda: replace(claim, limitation=" limitation"),
            lambda: replace(evidence, source_sha="A" * 40),
            lambda: replace(evidence, evidence_class="PHYSICAL"),
            lambda: replace(evidence, reported_status="QUALIFIED"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(Plan9ClaimedScopeError):
                    invalid()
        with self.assertRaisesRegex(Plan9ClaimedScopeError, "reserved"):
            ScopeClaim(
                capability="historical_replay",
                state=NOT_ACTIVATED,
                contract_digest=CONTRACT,
                limitation="not an external activation capability",
            )


if __name__ == "__main__":
    unittest.main()
