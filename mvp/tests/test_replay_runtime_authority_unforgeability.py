import hashlib
import hmac
import unittest

from mvp.autotrade_mvp.replay import (
    CausalReplay,
    ReplayError,
    ReplayEvent,
    RuntimeStateAuthority,
    RuntimeStateVerifier,
)


_REQUIRED_COMPONENTS = (
    "pending_event_queue",
    "rng_state",
    "strategy_state",
    "portfolio_accounting_state",
    "execution_state",
    "accrual_state",
    "policy_state",
    "instrument_state",
    "provider_state",
    "experiment_state",
)
_TRUSTED_SECRET = b"composition-pinned-runtime-authority-secret-0001"
_ATTACKER_SECRET = b"caller-selected-runtime-authority-secret-000001"


def _event(sequence, available_at, value):
    return ReplayEvent(
        sequence=sequence,
        available_at=available_at,
        source_version="v1",
        payload={"value": value},
    )


def _components(*, forged_rng=False):
    values = {
        name: hashlib.sha256(name.encode("utf-8")).hexdigest()
        for name in _REQUIRED_COMPONENTS
    }
    if forged_rng:
        values["rng_state"] = "f" * 64
    return values


def _signer(secret):
    def sign(material):
        return hmac.new(secret, material, hashlib.sha256).hexdigest()

    return sign


def _trusted_verifier():
    def verify(material, signature):
        expected = hmac.new(
            _TRUSTED_SECRET,
            material,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    return RuntimeStateVerifier(
        authority_id="runtime:production",
        verifier_id="host-trust:runtime-production-v1",
        verify_signature=verify,
    )


class RuntimeAuthorityUnforgeabilityTests(unittest.TestCase):
    def test_public_self_authored_authority_cannot_mint_resume_truth(self):
        events = [
            _event(1, "2026-09-24T10:00:00Z", 1),
            _event(2, "2026-09-24T10:01:00Z", 2),
        ]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        replay.advance_to("2026-09-24T10:00:00Z")

        forged_components = _components(forged_rng=True)

        def forged_cut():
            return "cut:caller-minted", replay.checkpoint(), forged_components

        attacker = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_ATTACKER_SECRET),
            cut_resolver=forged_cut,
        )

        # The verifier is a separate host-composition trust input.  The
        # self-authored signer cannot validate its own output merely because it
        # chose the same authority_id and controls the runtime-state resolver.
        with self.assertRaisesRegex(
            ReplayError,
            "snapshot authority signature mismatch",
        ):
            replay.composite_checkpoint(
                runtime_state_authority=attacker,
                runtime_state_verifier=_trusted_verifier(),
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )

    def test_trusted_signer_and_separately_provisioned_verifier_compose(self):
        events = [_event(1, "2026-09-24T10:00:00Z", 1)]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        components = _components()

        def trusted_cut():
            return "cut:trusted", replay.checkpoint(), components

        authority = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_TRUSTED_SECRET),
            cut_resolver=trusted_cut,
        )
        checkpoint = replay.composite_checkpoint(
            runtime_state_authority=authority,
            runtime_state_verifier=_trusted_verifier(),
            build_sha="a" * 64,
            protocol_ref="protocol:walk-forward-v1",
        )
        self.assertEqual(
            checkpoint.runtime_verifier_id,
            "host-trust:runtime-production-v1",
        )


if __name__ == "__main__":
    unittest.main()
