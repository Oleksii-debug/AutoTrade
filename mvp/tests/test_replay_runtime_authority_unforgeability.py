import hashlib
import hmac
import weakref
import unittest

from mvp.autotrade_mvp.replay import (
    CausalReplay,
    CompositeReplayCheckpoint,
    ReplayCheckpoint,
    ReplayError,
    ReplayEvent,
    RuntimeStateAuthority,
    RuntimeStateSnapshot,
    RuntimeStateVerifier,
    resume_from_composite_checkpoint,
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


def _signature_verifier(secret):
    def verify(material, signature):
        expected = hmac.new(
            secret,
            material,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    return verify


def _trusted_verifier():
    return RuntimeStateVerifier.select_product_trust(
        authority_id="runtime:production",
        verifier_id="host-trust:runtime-production-v1",
        verify_signature=_signature_verifier(_TRUSTED_SECRET),
    )


class _HostileText(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.touched = False
        return instance

    def strip(self, *args, **kwargs):
        self.touched = True
        raise AssertionError("hostile text callback executed")

    def __iter__(self):
        self.touched = True
        raise AssertionError("hostile text iteration executed")


class _HostileComponents(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.touched = False

    def copy(self):
        self.touched = True
        raise AssertionError("hostile component copy executed")

    def items(self):
        self.touched = True
        raise AssertionError("hostile component iteration executed")

    def __iter__(self):
        self.touched = True
        raise AssertionError("hostile component iteration executed")


class RuntimeAuthorityUnforgeabilityTests(unittest.TestCase):
    def _checkpoint_value(self):
        return ReplayCheckpoint(
            dataset_digest="d" * 64,
            cursor=0,
            clock="2026-09-24T09:59:00Z",
        )

    def test_runtime_trust_text_rejects_subclass_before_callback(self):
        hostile = _HostileText("runtime:hostile")
        with self.assertRaisesRegex(ReplayError, "authority_id must be non-empty"):
            RuntimeStateAuthority(
                authority_id=hostile,
                signer=_signer(_TRUSTED_SECRET),
                cut_resolver=lambda: (
                    "cut:unused",
                    self._checkpoint_value(),
                    _components(),
                ),
            )
        self.assertFalse(hostile.touched)

        hostile = _HostileText("runtime:hostile")
        with self.assertRaisesRegex(ReplayError, "authority_id must be non-empty"):
            RuntimeStateVerifier(
                authority_id=hostile,
                verifier_id="verifier:unused",
                verify_signature=_signature_verifier(_TRUSTED_SECRET),
            )
        self.assertFalse(hostile.touched)

    def test_runtime_component_container_rejects_subclass_before_callback(self):
        hostile = _HostileComponents(_components())
        snapshot = {
            "cut_id": "cut:hostile-components",
            "replay": self._checkpoint_value(),
            "runtime_components": hostile,
            "authority_id": "runtime:components",
            "verifier_id": "verifier:components",
            "authority_seal": "a" * 64,
        }
        with self.assertRaisesRegex(TypeError, "exact dictionary"):
            RuntimeStateSnapshot(**snapshot)
        self.assertFalse(hostile.touched)

    def test_runtime_authority_resolver_cannot_dispatch_component_subclass(self):
        hostile = _HostileComponents(_components())
        replay = self._checkpoint_value()

        def hostile_cut():
            return "cut:resolver", replay, hostile

        authority = RuntimeStateAuthority(
            authority_id="runtime:resolver-exact-ingress",
            signer=_signer(_TRUSTED_SECRET),
            cut_resolver=hostile_cut,
        )
        with self.assertRaisesRegex(TypeError, "exact dictionary"):
            RuntimeStateAuthority.capture(
                authority,
                verifier_id="verifier:resolver-exact-ingress",
            )
        self.assertFalse(hostile.touched)

    def test_runtime_snapshot_rejects_replay_subclass_before_field_reads(self):
        touched = []

        class HostileReplayCheckpoint(ReplayCheckpoint):
            def __getattribute__(self, name):
                if name in {"dataset_digest", "cursor", "clock"}:
                    touched.append(name)
                return super().__getattribute__(name)

        hostile = HostileReplayCheckpoint(
            dataset_digest="e" * 64,
            cursor=0,
            clock="2026-09-24T09:59:00Z",
        )
        touched.clear()
        with self.assertRaisesRegex(TypeError, "exact ReplayCheckpoint"):
            RuntimeStateSnapshot(
                cut_id="cut:hostile-replay",
                replay=hostile,
                runtime_components=_components(),
                authority_id="runtime:hostile-replay",
                verifier_id="verifier:hostile-replay",
                authority_seal="a" * 64,
            )
        self.assertEqual(touched, [])

    def test_composite_document_rejects_text_subclass_before_callback(self):
        hostile = _HostileText("{}")
        with self.assertRaisesRegex(TypeError, "exact text"):
            CompositeReplayCheckpoint.from_canonical_json(hostile)
        self.assertFalse(hostile.touched)

    def test_composite_parser_rejects_subclass_factory_before_construction(self):
        touched = []

        class HostileCompositeCheckpoint(CompositeReplayCheckpoint):
            def __post_init__(self):
                touched.append("constructed")
                super().__post_init__()

        with self.assertRaisesRegex(TypeError, "parser requires canonical class"):
            HostileCompositeCheckpoint.from_canonical_json("{}")
        self.assertEqual(touched, [])

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

    def test_unselected_attacker_pair_fails_closed_before_checkpoint(self):
        events = [
            _event(1, "2026-09-24T10:00:00Z", 1),
            _event(2, "2026-09-24T10:01:00Z", 2),
        ]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        replay.advance_to("2026-09-24T10:00:00Z")
        forged_components = _components(forged_rng=True)

        def forged_cut():
            return "cut:attacker-only", replay.checkpoint(), forged_components

        attacker = RuntimeStateAuthority(
            authority_id="runtime:attacker-only",
            signer=_signer(_ATTACKER_SECRET),
            cut_resolver=forged_cut,
        )
        attacker_verifier = RuntimeStateVerifier(
            authority_id="runtime:attacker-only",
            verifier_id="attacker:self-trust-v1",
            verify_signature=_signature_verifier(_ATTACKER_SECRET),
        )

        with self.assertRaisesRegex(
            ReplayError,
            "product-selected runtime verifier is unavailable",
        ):
            replay.composite_checkpoint(
                runtime_state_authority=attacker,
                runtime_state_verifier=attacker_verifier,
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )
        self.assertEqual(replay.cursor, 1)

    def test_competing_product_trust_selection_is_rejected_while_anchor_live(self):
        product_verifier = _trusted_verifier()
        self.assertEqual(product_verifier.authority_id, "runtime:production")

        with self.assertRaisesRegex(
            ReplayError,
            "already has a live product-selected verifier",
        ):
            RuntimeStateVerifier.select_product_trust(
                authority_id="runtime:production",
                verifier_id="attacker:self-trust-v1",
                verify_signature=_signature_verifier(_ATTACKER_SECRET),
            )

        self.assertEqual(
            product_verifier.verifier_id,
            "host-trust:runtime-production-v1",
        )

    def test_attacker_signer_verifier_resolver_pair_cannot_mint_checkpoint(self):
        events = [
            _event(1, "2026-09-24T10:00:00Z", 1),
            _event(2, "2026-09-24T10:01:00Z", 2),
        ]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        replay.advance_to("2026-09-24T10:00:00Z")
        product_verifier = _trusted_verifier()
        forged_components = _components(forged_rng=True)

        def forged_cut():
            return "cut:caller-minted", replay.checkpoint(), forged_components

        attacker = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_ATTACKER_SECRET),
            cut_resolver=forged_cut,
        )
        attacker_verifier = RuntimeStateVerifier(
            authority_id="runtime:production",
            verifier_id="host-trust:runtime-production-v1",
            verify_signature=_signature_verifier(_ATTACKER_SECRET),
        )

        self.assertEqual(product_verifier.authority_id, "runtime:production")
        with self.assertRaisesRegex(
            ReplayError,
            "not the product-selected trust anchor",
        ):
            replay.composite_checkpoint(
                runtime_state_authority=attacker,
                runtime_state_verifier=attacker_verifier,
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )
        self.assertEqual(replay.cursor, 1)

    def test_attacker_verifier_cannot_resume_product_checkpoint(self):
        events = [
            _event(1, "2026-09-24T10:00:00Z", 1),
            _event(2, "2026-09-24T10:01:00Z", 2),
        ]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        replay.advance_to("2026-09-24T10:00:00Z")
        components = _components()
        product_verifier = _trusted_verifier()

        def trusted_cut():
            return "cut:trusted", replay.checkpoint(), components

        trusted_authority = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_TRUSTED_SECRET),
            cut_resolver=trusted_cut,
        )
        checkpoint = replay.composite_checkpoint(
            runtime_state_authority=trusted_authority,
            runtime_state_verifier=product_verifier,
            build_sha="a" * 64,
            protocol_ref="protocol:walk-forward-v1",
        )

        def forged_cut():
            return checkpoint.runtime_cut_id, checkpoint.replay, dict(checkpoint.runtime_components)

        attacker = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_ATTACKER_SECRET),
            cut_resolver=forged_cut,
        )
        attacker_verifier = RuntimeStateVerifier(
            authority_id="runtime:production",
            verifier_id="host-trust:runtime-production-v1",
            verify_signature=_signature_verifier(_ATTACKER_SECRET),
        )
        with self.assertRaisesRegex(
            ReplayError,
            "not the product-selected trust anchor",
        ):
            resume_from_composite_checkpoint(
                events,
                start_at="2026-09-24T09:59:00Z",
                checkpoint=checkpoint,
                runtime_state_authority=attacker,
                runtime_state_verifier=attacker_verifier,
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )
        self.assertEqual(replay.cursor, 1)

    def test_runtime_trust_registries_expose_no_removal_callbacks(self):
        replay = CausalReplay(
            [_event(1, "2026-09-24T10:00:00Z", 1)],
            start_at="2026-09-24T09:59:00Z",
        )
        components = _components()

        def trusted_cut():
            return "cut:trusted", replay.checkpoint(), components

        authority = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_TRUSTED_SECRET),
            cut_resolver=trusted_cut,
        )
        verifier = _trusted_verifier()

        authority_callbacks = [
            item.__callback__
            for item in weakref.getweakrefs(authority)
            if item.__callback__ is not None
        ]
        verifier_callbacks = [
            item.__callback__
            for item in weakref.getweakrefs(verifier)
            if item.__callback__ is not None
        ]
        self.assertEqual(authority_callbacks, [])
        self.assertEqual(verifier_callbacks, [])

    def test_trusted_verifier_binding_is_one_shot(self):
        events = [_event(1, "2026-09-24T10:00:00Z", 1)]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        forged_components = _components(forged_rng=True)

        def forged_cut():
            return "cut:caller-minted", replay.checkpoint(), forged_components

        attacker = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_ATTACKER_SECRET),
            cut_resolver=forged_cut,
        )
        verifier = _trusted_verifier()

        with self.assertRaisesRegex(ReplayError, "already initialized"):
            RuntimeStateVerifier.__init__(
                verifier,
                authority_id="runtime:production",
                verifier_id="host-trust:runtime-production-v1",
                verify_signature=lambda _material, _signature: True,
            )

        with self.assertRaisesRegex(
            ReplayError,
            "snapshot authority signature mismatch",
        ):
            replay.composite_checkpoint(
                runtime_state_authority=attacker,
                runtime_state_verifier=verifier,
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )

    def test_runtime_authority_binding_is_one_shot(self):
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

        with self.assertRaisesRegex(ReplayError, "already initialized"):
            RuntimeStateAuthority.__init__(
                authority,
                authority_id="runtime:production",
                signer=_signer(_ATTACKER_SECRET),
                cut_resolver=lambda: (
                    "cut:rebound",
                    replay.checkpoint(),
                    _components(forged_rng=True),
                ),
            )

        checkpoint = replay.composite_checkpoint(
            runtime_state_authority=authority,
            runtime_state_verifier=_trusted_verifier(),
            build_sha="a" * 64,
            protocol_ref="protocol:walk-forward-v1",
        )
        self.assertEqual(dict(checkpoint.runtime_components), components)

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
