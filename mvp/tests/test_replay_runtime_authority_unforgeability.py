import hashlib
import unittest

from mvp.autotrade_mvp.replay import (
    CausalReplay,
    ReplayError,
    ReplayEvent,
    RuntimeStateAuthority,
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

        # RuntimeStateAuthority is documented as composition-owned authority.
        # A public caller must not be able to become that authority merely by
        # choosing an authority_id, secret and resolver, then using the same
        # object to mint and verify a checkpoint it controls end-to-end.
        attacker = RuntimeStateAuthority(
            authority_id="runtime:production",
            secret=b"caller-selected-runtime-authority-secret-0001",
            cut_resolver=forged_cut,
        )
        with self.assertRaisesRegex(
            ReplayError,
            "not composition-issued",
        ):
            replay.composite_checkpoint(
                runtime_state_authority=attacker,
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )


if __name__ == "__main__":
    unittest.main()
