from __future__ import annotations

import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import research_runtime_binding as binding
from mvp.autotrade_mvp.replay import (
    CompositeReplayCheckpoint,
    ReplayCheckpoint,
    RuntimeStateVerifier,
)


def _components() -> dict[str, str]:
    return {
        name: (format(index, "064x"))
        for index, name in enumerate(
            (
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
            ),
            start=1,
        )
    }


def _checkpoint() -> CompositeReplayCheckpoint:
    return CompositeReplayCheckpoint(
        replay=ReplayCheckpoint(
            dataset_digest="d" * 64,
            cursor=7,
            clock="2026-10-06T12:00:00Z",
        ),
        runtime_components=_components(),
        runtime_cut_id="cut:section5",
        runtime_authority_id="runtime:section5",
        runtime_verifier_id="verifier:section5",
        runtime_authority_seal="a" * 64,
        build_sha="b" * 40,
        protocol_ref="protocol:section5",
    )


def _verifier() -> RuntimeStateVerifier:
    return RuntimeStateVerifier(
        authority_id="runtime:section5",
        verifier_id="verifier:section5",
        verify_signature=lambda material, signature: True,
    )


class Section5RuntimeFoldBindingTests(unittest.TestCase):
    def test_runtime_verification_precedes_any_research_fit(self):
        checkpoint = _checkpoint()
        verifier = _verifier()
        with (
            patch.object(
                RuntimeStateVerifier,
                "verify_checkpoint_binding",
                side_effect=RuntimeError("checkpoint rejected"),
            ) as verify,
            patch.object(
                binding,
                "fit_authoritative_fold_normalizer",
                side_effect=AssertionError("research fit ran before runtime verification"),
            ) as fit,
        ):
            with self.assertRaisesRegex(RuntimeError, "checkpoint rejected"):
                binding.fit_authoritative_fold_at_verified_runtime_cut(
                    checkpoint=checkpoint,
                    verifier=verifier,
                    registry=object(),
                    dataset_id="dataset",
                    dataset_version=1,
                    manifest_digest="sha256:" + "c" * 64,
                    artifact_store=object(),
                    fold=object(),
                    spec=object(),
                )
        self.assertEqual(verify.call_count, 1)
        fit.assert_not_called()

    def test_fit_receives_fingerprint_of_detached_verified_checkpoint(self):
        checkpoint = _checkpoint()
        expected = checkpoint.fingerprint
        verifier = _verifier()
        marker = object()
        verified_objects = []

        def verify(selected, candidate):
            self.assertIs(selected, verifier)
            self.assertIsNot(candidate, checkpoint)
            verified_objects.append(candidate)
            object.__setattr__(checkpoint, "build_sha", "c" * 40)

        with (
            patch.object(
                RuntimeStateVerifier,
                "verify_checkpoint_binding",
                side_effect=verify,
            ),
            patch.object(
                binding,
                "fit_authoritative_fold_normalizer",
                return_value=marker,
            ) as fit,
        ):
            result = binding.fit_authoritative_fold_at_verified_runtime_cut(
                checkpoint=checkpoint,
                verifier=verifier,
                registry=object(),
                dataset_id="dataset",
                dataset_version=1,
                manifest_digest="sha256:" + "e" * 64,
                artifact_store=object(),
                fold=object(),
                spec=object(),
            )

        self.assertIs(result, marker)
        self.assertEqual(len(verified_objects), 1)
        self.assertEqual(
            fit.call_args.kwargs["replay_common_cut_fingerprint"],
            expected,
        )
        self.assertNotEqual(checkpoint.fingerprint, expected)

    def test_checkpoint_and_verifier_subclasses_fail_before_callbacks(self):
        class HostileCheckpoint(CompositeReplayCheckpoint):
            pass

        class HostileVerifier(RuntimeStateVerifier):
            pass

        base = _checkpoint()
        hostile_checkpoint = HostileCheckpoint(
            replay=base.replay,
            runtime_components=dict(base.runtime_components),
            runtime_cut_id=base.runtime_cut_id,
            runtime_authority_id=base.runtime_authority_id,
            runtime_verifier_id=base.runtime_verifier_id,
            runtime_authority_seal=base.runtime_authority_seal,
            build_sha=base.build_sha,
            protocol_ref=base.protocol_ref,
        )
        with patch.object(
            RuntimeStateVerifier,
            "verify_checkpoint_binding",
            side_effect=AssertionError("verification callback must not run"),
        ):
            with self.assertRaisesRegex(TypeError, "exact CompositeReplayCheckpoint"):
                binding._verified_runtime_cut(hostile_checkpoint, _verifier())

        hostile_verifier = HostileVerifier(
            authority_id="runtime:section5",
            verifier_id="verifier:section5",
            verify_signature=lambda material, signature: True,
        )
        with patch.object(
            RuntimeStateVerifier,
            "verify_checkpoint_binding",
            side_effect=AssertionError("verification callback must not run"),
        ):
            with self.assertRaisesRegex(TypeError, "exact RuntimeStateVerifier"):
                binding._verified_runtime_cut(_checkpoint(), hostile_verifier)


if __name__ == "__main__":
    unittest.main()
