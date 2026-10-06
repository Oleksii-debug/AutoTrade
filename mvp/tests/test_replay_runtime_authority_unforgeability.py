import gc
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


_TRUSTED_VERIFY_SIGNATURE = _signature_verifier(_TRUSTED_SECRET)


_TRUSTED_VERIFIER_REF = None


def _trusted_verifier():
    global _TRUSTED_VERIFIER_REF
    selected = _TRUSTED_VERIFIER_REF() if _TRUSTED_VERIFIER_REF else None
    if selected is not None:
        return selected
    selected = RuntimeStateVerifier.select_product_trust(
        authority_id="runtime:production",
        verifier_id="host-trust:runtime-production-v1",
        verify_signature=_TRUSTED_VERIFY_SIGNATURE,
    )
    _TRUSTED_VERIFIER_REF = weakref.ref(selected)
    return selected


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


class _HostileComparable:
    def __init__(self):
        self.touched = False

    def __eq__(self, other):
        self.touched = True
        raise AssertionError("hostile equality callback executed")

    def __ne__(self, other):
        self.touched = True
        raise AssertionError("hostile inequality callback executed")


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

    def test_runtime_snapshot_detaches_caller_owned_replay_value(self):
        replay = self._checkpoint_value()
        snapshot = RuntimeStateSnapshot(
            cut_id="cut:detached-replay",
            replay=replay,
            runtime_components=_components(),
            authority_id="runtime:detached-replay",
            verifier_id="verifier:detached-replay",
            authority_seal="a" * 64,
        )

        object.__setattr__(replay, "dataset_digest", "f" * 64)
        object.__setattr__(replay, "cursor", 7)
        object.__setattr__(replay, "clock", "2030-01-01T00:00:00Z")

        self.assertIsNot(snapshot.replay, replay)
        self.assertEqual(snapshot.replay.dataset_digest, "d" * 64)
        self.assertEqual(snapshot.replay.cursor, 0)
        self.assertEqual(snapshot.replay.clock, "2026-09-24T09:59:00Z")

    def test_runtime_snapshot_revalidates_mutated_exact_replay(self):
        replay = self._checkpoint_value()
        object.__setattr__(replay, "cursor", -1)

        with self.assertRaisesRegex(ReplayError, "cursor must be non-negative"):
            RuntimeStateSnapshot(
                cut_id="cut:invalid-replay",
                replay=replay,
                runtime_components=_components(),
                authority_id="runtime:invalid-replay",
                verifier_id="verifier:invalid-replay",
                authority_seal="a" * 64,
            )

    def test_composite_checkpoint_detaches_caller_owned_replay_value(self):
        replay = self._checkpoint_value()
        checkpoint = CompositeReplayCheckpoint(
            replay=replay,
            runtime_components=_components(),
            runtime_cut_id="cut:detached-composite",
            runtime_authority_id="runtime:detached-composite",
            runtime_verifier_id="verifier:detached-composite",
            runtime_authority_seal="a" * 64,
            build_sha="b" * 40,
            protocol_ref="protocol:detached-composite",
        )
        fingerprint = checkpoint.fingerprint

        object.__setattr__(replay, "dataset_digest", "f" * 64)
        object.__setattr__(replay, "cursor", 9)
        object.__setattr__(replay, "clock", "2030-01-01T00:00:00Z")

        self.assertIsNot(checkpoint.replay, replay)
        self.assertEqual(checkpoint.replay.dataset_digest, "d" * 64)
        self.assertEqual(checkpoint.replay.cursor, 0)
        self.assertEqual(checkpoint.replay.clock, "2026-09-24T09:59:00Z")
        self.assertEqual(checkpoint.fingerprint, fingerprint)

    def test_verifier_revalidates_mutated_snapshot_before_identity_comparison(self):
        verifier = _trusted_verifier()
        snapshot = RuntimeStateSnapshot(
            cut_id="cut:mutated-snapshot",
            replay=self._checkpoint_value(),
            runtime_components=_components(),
            authority_id="runtime:production",
            verifier_id="host-trust:runtime-production-v1",
            authority_seal="a" * 64,
        )
        hostile = _HostileComparable()
        object.__setattr__(snapshot, "authority_id", hostile)

        with self.assertRaisesRegex(ReplayError, "authority_id must be non-empty"):
            RuntimeStateVerifier.verify_snapshot(verifier, snapshot)
        self.assertFalse(hostile.touched)

    def test_verifier_revalidates_mutated_checkpoint_before_identity_comparison(self):
        verifier = _trusted_verifier()
        checkpoint = CompositeReplayCheckpoint(
            replay=self._checkpoint_value(),
            runtime_components=_components(),
            runtime_cut_id="cut:mutated-checkpoint",
            runtime_authority_id="runtime:production",
            runtime_verifier_id="host-trust:runtime-production-v1",
            runtime_authority_seal="a" * 64,
            build_sha="b" * 40,
            protocol_ref="protocol:mutated-checkpoint",
        )
        hostile = _HostileComparable()
        object.__setattr__(checkpoint, "runtime_authority_id", hostile)

        with self.assertRaisesRegex(
            ReplayError,
            "runtime_authority_id must be non-empty",
        ):
            RuntimeStateVerifier.verify_checkpoint_binding(verifier, checkpoint)
        self.assertFalse(hostile.touched)

    def test_authority_revalidates_mutated_snapshot_before_identity_comparison(self):
        replay = self._checkpoint_value()
        authority = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=_signer(_TRUSTED_SECRET),
            cut_resolver=lambda: ("cut:unused", replay, _components()),
        )
        snapshot = RuntimeStateSnapshot(
            cut_id="cut:mutated-seal",
            replay=replay,
            runtime_components=_components(),
            authority_id="runtime:production",
            verifier_id="host-trust:runtime-production-v1",
            authority_seal="a" * 64,
        )
        hostile = _HostileComparable()
        object.__setattr__(snapshot, "authority_id", hostile)

        with self.assertRaisesRegex(ReplayError, "authority_id must be non-empty"):
            RuntimeStateAuthority.seal_checkpoint(
                authority,
                snapshot,
                verifier=_trusted_verifier(),
                build_sha="b" * 40,
                protocol_ref="protocol:mutated-seal",
            )
        self.assertFalse(hostile.touched)

    def test_checkpoint_sealer_rejects_caller_constructed_snapshot_before_signing(self):
        signer_calls = []

        def counted_signer(material):
            signer_calls.append(material)
            return _signer(_TRUSTED_SECRET)(material)

        authority = RuntimeStateAuthority(
            authority_id="runtime:production",
            signer=counted_signer,
            cut_resolver=lambda: (
                "cut:unused",
                self._checkpoint_value(),
                _components(),
            ),
        )
        forged = RuntimeStateSnapshot(
            cut_id="cut:forged-signing-oracle",
            replay=self._checkpoint_value(),
            runtime_components=_components(forged_rng=True),
            authority_id="runtime:production",
            verifier_id="host-trust:runtime-production-v1",
            authority_seal="a" * 64,
        )

        with self.assertRaisesRegex(
            ReplayError,
            "snapshot authority signature mismatch",
        ):
            RuntimeStateAuthority.seal_checkpoint(
                authority,
                forged,
                verifier=_trusted_verifier(),
                build_sha="b" * 40,
                protocol_ref="protocol:forged-signing-oracle",
            )
        self.assertEqual(signer_calls, [])

    def test_composite_schema_version_rejects_text_subclass(self):
        hostile = _HostileText("4.0.0")
        with self.assertRaisesRegex(
            TypeError,
            "schema_version must be exact text",
        ):
            CompositeReplayCheckpoint(
                replay=self._checkpoint_value(),
                runtime_components=_components(),
                runtime_cut_id="cut:hostile-schema",
                runtime_authority_id="runtime:hostile-schema",
                runtime_verifier_id="verifier:hostile-schema",
                runtime_authority_seal="a" * 64,
                build_sha="b" * 40,
                protocol_ref="protocol:hostile-schema",
                schema_version=hostile,
            )
        self.assertFalse(hostile.touched)

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

    def test_product_trust_binding_cannot_be_replaced_after_verifier_gc(self):
        verifier = _trusted_verifier()
        verifier_ref = weakref.ref(verifier)
        del verifier
        gc.collect()
        self.assertIsNone(verifier_ref())

        with self.assertRaisesRegex(
            ReplayError,
            "product-selected verifier binding is immutable",
        ):
            RuntimeStateVerifier.select_product_trust(
                authority_id="runtime:production",
                verifier_id="host-trust:runtime-production-v1",
                verify_signature=_signature_verifier(_ATTACKER_SECRET),
            )

        rebound = _trusted_verifier()
        self.assertEqual(
            rebound.verifier_id,
            "host-trust:runtime-production-v1",
        )

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
            "not the product-selected trust anchor|immutable product trust binding",
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
            "not the product-selected trust anchor|immutable product trust binding",
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

    def test_dead_unselected_authority_state_releases_callbacks_on_next_registration(self):
        replay = self._checkpoint_value()

        def ephemeral_signer(material):
            return _signer(_TRUSTED_SECRET)(material)

        def ephemeral_cut():
            return "cut:ephemeral", replay, _components()

        signer_ref = weakref.ref(ephemeral_signer)
        cut_ref = weakref.ref(ephemeral_cut)
        authority = RuntimeStateAuthority(
            authority_id="runtime:ephemeral-authority",
            signer=ephemeral_signer,
            cut_resolver=ephemeral_cut,
        )
        del ephemeral_signer
        del ephemeral_cut
        del authority
        gc.collect()

        self.assertIsNotNone(signer_ref())
        self.assertIsNotNone(cut_ref())

        survivor = RuntimeStateAuthority(
            authority_id="runtime:authority-registry-survivor",
            signer=_signer(_TRUSTED_SECRET),
            cut_resolver=lambda: ("cut:survivor", replay, _components()),
        )
        gc.collect()

        self.assertIsNone(signer_ref())
        self.assertIsNone(cut_ref())
        self.assertEqual(
            survivor.authority_id,
            "runtime:authority-registry-survivor",
        )

    def test_dead_unselected_verifier_state_releases_callback_on_next_registration(self):
        verify = _signature_verifier(_ATTACKER_SECRET)
        verify_ref = weakref.ref(verify)
        verifier = RuntimeStateVerifier(
            authority_id="runtime:ephemeral-verifier",
            verifier_id="verifier:ephemeral",
            verify_signature=verify,
        )
        del verify
        del verifier
        gc.collect()

        self.assertIsNotNone(verify_ref())

        survivor = RuntimeStateVerifier(
            authority_id="runtime:verifier-registry-survivor",
            verifier_id="verifier:survivor",
            verify_signature=_signature_verifier(_TRUSTED_SECRET),
        )
        gc.collect()

        self.assertIsNone(verify_ref())
        self.assertEqual(
            survivor.verifier_id,
            "verifier:survivor",
        )

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
