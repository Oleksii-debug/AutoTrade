import unittest

from mvp.autotrade_mvp.bounded_real import (
    ArtifactStoreEvidenceVerifier,
    BoundedRealEnvelope,
    BoundedRealObservations,
    assess_bounded_real_qualification,
)


class BoundedRealAuthorityUnforgeabilityTests(unittest.TestCase):
    @staticmethod
    def _envelope() -> BoundedRealEnvelope:
        return BoundedRealEnvelope.create(
            envelope_id="bounded-real-test",
            source_sha="a" * 40,
            account_id="acct-1",
            provider_id="PROVIDER-A",
            policy_id="policy-1",
            allowed_actions=frozenset({"ORDER.SUBMIT"}),
            max_capital="1000",
            max_single_notional="100",
            max_gross_leverage="2",
        )

    @staticmethod
    def _observations(envelope: BoundedRealEnvelope) -> BoundedRealObservations:
        return BoundedRealObservations.create(
            source_sha=envelope.source_sha,
            envelope_id=envelope.envelope_id,
            envelope_digest=envelope.envelope_digest,
            provider_id=envelope.provider_id,
            account_id=envelope.account_id,
            observed_fill_count=0,
            observed_partial_fill=False,
            all_fills_reconciled=True,
            fees_reconciled=True,
            revocation_verified=True,
            protection_verified=True,
            unauthorized_action_count=0,
            unresolved_unknown_count=0,
            evidence_refs=(),
        )

    def test_envelope_subclass_is_rejected_before_financial_scope_dispatch(self):
        calls = []

        class HostileEnvelope(BoundedRealEnvelope):
            def __getattribute__(self, name):
                if name in {
                    "source_sha",
                    "envelope_id",
                    "envelope_digest",
                    "provider_id",
                    "account_id",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        hostile = HostileEnvelope(
            envelope_id="bounded-real-test",
            source_sha="a" * 40,
            account_id="acct-1",
            provider_id="PROVIDER-A",
            policy_id="policy-1",
            allowed_actions=frozenset({"ORDER.SUBMIT"}),
            max_capital="1000",
            max_single_notional="100",
            max_gross_leverage="2",
        )
        observations = self._observations(self._envelope())
        calls.clear()

        with self.assertRaises(TypeError):
            assess_bounded_real_qualification(
                envelope=hostile,
                prerequisite_evidence=(),
                observations=observations,
            )

        self.assertEqual(calls, [])

    def test_verifier_subclass_is_rejected_before_verdict_dispatch(self):
        envelope = self._envelope()
        observations = self._observations(envelope)
        calls = []

        class HostileVerifier(ArtifactStoreEvidenceVerifier):
            def __init__(self):
                pass

            @property
            def identity(self):
                calls.append("identity")
                return "FORGED"

            def verify(self, _ref):
                calls.append("verify")
                raise AssertionError("hostile verifier callback must not run")

        with self.assertRaises(TypeError):
            assess_bounded_real_qualification(
                envelope=envelope,
                prerequisite_evidence=(),
                observations=observations,
                evidence_verifier=HostileVerifier(),
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
