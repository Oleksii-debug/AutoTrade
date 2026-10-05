import unittest

from mvp.autotrade_mvp.bounded_real import (
    BoundedRealEnvelope,
    BoundedRealObservations,
    assess_bounded_real_qualification,
)
from mvp.tests.test_bounded_real import (
    envelope,
    observations,
    prerequisites,
    valid_verifier,
)


class _ChurningEnvelope(BoundedRealEnvelope):
    """Return a safe account only after the terminal boundary captured a mismatch."""

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "_snapshot_reads", 0)
        object.__setattr__(self, "_snapshot_ready", True)

    def __getattribute__(self, name):
        if name == "account_id":
            state = object.__getattribute__(self, "__dict__")
            if state.get("_snapshot_ready", False):
                reads = state["_snapshot_reads"]
                object.__setattr__(self, "_snapshot_reads", reads + 1)
                return "attacker-account" if reads == 0 else "account-1"
        return super().__getattribute__(name)


class _ChurningObservations(BoundedRealObservations):
    """Hide an unauthorized action only after the first terminal-boundary read."""

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "_snapshot_reads", 0)
        object.__setattr__(self, "_snapshot_ready", True)

    def __getattribute__(self, name):
        if name == "unauthorized_action_count":
            state = object.__getattribute__(self, "__dict__")
            if state.get("_snapshot_ready", False):
                reads = state["_snapshot_reads"]
                object.__setattr__(self, "_snapshot_reads", reads + 1)
                return 1 if reads == 0 else 0
        return super().__getattribute__(name)


def _churning_envelope() -> _ChurningEnvelope:
    value = envelope()
    return _ChurningEnvelope(
        envelope_id=value.envelope_id,
        source_sha=value.source_sha,
        account_id=value.account_id,
        provider_id=value.provider_id,
        policy_id=value.policy_id,
        allowed_actions=value.allowed_actions,
        max_capital=value.max_capital,
        max_single_notional=value.max_single_notional,
        max_gross_leverage=value.max_gross_leverage,
    )


def _churning_observations() -> _ChurningObservations:
    value = observations()
    return _ChurningObservations(
        source_sha=value.source_sha,
        envelope_id=value.envelope_id,
        envelope_digest=value.envelope_digest,
        provider_id=value.provider_id,
        account_id=value.account_id,
        observed_fill_count=value.observed_fill_count,
        observed_partial_fill=value.observed_partial_fill,
        all_fills_reconciled=value.all_fills_reconciled,
        fees_reconciled=value.fees_reconciled,
        revocation_verified=value.revocation_verified,
        protection_verified=value.protection_verified,
        unauthorized_action_count=value.unauthorized_action_count,
        unresolved_unknown_count=value.unresolved_unknown_count,
        evidence_refs=value.evidence_refs,
    )


class BoundedRealTerminalSnapshotTests(unittest.TestCase):
    def test_envelope_caller_graph_is_captured_once_before_terminal_decision(self):
        attacker = _churning_envelope()
        result = assess_bounded_real_qualification(
            envelope=attacker,
            prerequisite_evidence=prerequisites(),
            observations=observations(),
            evidence_verifier=valid_verifier,
        )

        self.assertFalse(result.complete)
        self.assertIn("observation_scope_mismatch", result.reason_codes)
        self.assertTrue(
            any(
                reason.startswith("provider_account_mismatch:")
                for reason in result.reason_codes
            )
        )
        self.assertEqual(attacker._snapshot_reads, 1)

    def test_observation_caller_graph_cannot_hide_post_snapshot_safety_event(self):
        attacker = _churning_observations()
        result = assess_bounded_real_qualification(
            envelope=envelope(),
            prerequisite_evidence=prerequisites(),
            observations=attacker,
            evidence_verifier=valid_verifier,
        )

        self.assertFalse(result.complete)
        self.assertIn("unauthorized_action_observed", result.reason_codes)
        self.assertEqual(attacker._snapshot_reads, 1)


if __name__ == "__main__":
    unittest.main()
