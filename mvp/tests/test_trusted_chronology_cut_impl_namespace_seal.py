from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import trusted_chronology_cut as chronology


class TrustedChronologyCutImplementationNamespaceSealTests(unittest.TestCase):
    def test_guard_rejects_pre_call_authority_rebinding(self) -> None:
        canonical = object()
        namespace = {"authority": canonical, "__name__": "test"}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )

        namespace["authority"] = object()

        with self.assertRaisesRegex(
            RuntimeError,
            "trusted chronology implementation authority changed: authority",
        ):
            guard()

    def test_production_current_cut_rejects_pre_call_verifier_rebinding(self) -> None:
        forged = Mock()
        with (
            patch.object(
                chronology,
                "verify_canonical_qualification_attestation",
                forged,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology implementation authority changed: "
                "verify_canonical_qualification_attestation",
            ),
        ):
            chronology.require_current_trusted_chronology_cut()

        forged.assert_not_called()

    def test_guard_ignores_only_explicitly_excluded_names(self) -> None:
        canonical = object()
        namespace = {"authority": canonical, "diagnostic": object()}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset({"diagnostic"}),
        )

        namespace["diagnostic"] = object()
        guard()
        namespace["authority"] = object()

        with self.assertRaisesRegex(RuntimeError, "authority changed: authority"):
            guard()

    def test_rebinding_during_current_cut_verification_fails_before_post_checks(self) -> None:
        canonical = object()
        namespace = {"authority": canonical}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )
        durable = object()
        post = Mock()
        horizon = Mock()

        def current(**_kwargs):
            namespace["authority"] = object()
            return durable

        verifier = chronology._build_current_cut_with_horizon(
            require_current_cut=current,
            require_post_currentness=post,
            require_horizon=horizon,
            require_impl_namespace_sealed=guard,
        )

        with self.assertRaisesRegex(RuntimeError, "authority changed: authority"):
            verifier(claimed_instants=("2026-10-03T20:00:00Z",))

        post.assert_not_called()
        horizon.assert_not_called()

    def test_rebinding_during_post_currentness_fails_before_horizon(self) -> None:
        canonical = object()
        namespace = {"authority": canonical}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )
        durable = object()
        horizon = Mock()

        def post(_durable, *, kwargs):
            del kwargs
            namespace["authority"] = object()

        verifier = chronology._build_current_cut_with_horizon(
            require_current_cut=Mock(return_value=durable),
            require_post_currentness=post,
            require_horizon=horizon,
            require_impl_namespace_sealed=guard,
        )

        with self.assertRaisesRegex(RuntimeError, "authority changed: authority"):
            verifier(claimed_instants=("2026-10-03T20:00:00Z",))

        horizon.assert_not_called()

    def test_rebinding_during_horizon_check_fails_before_acceptance_returns(self) -> None:
        canonical = object()
        namespace = {"authority": canonical}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )
        durable = object()

        def horizon(_durable, *_instants):
            namespace["authority"] = object()

        verifier = chronology._build_current_cut_with_horizon(
            require_current_cut=Mock(return_value=durable),
            require_post_currentness=Mock(),
            require_horizon=horizon,
            require_impl_namespace_sealed=guard,
        )

        with self.assertRaisesRegex(RuntimeError, "authority changed: authority"):
            verifier(claimed_instants=("2026-10-03T20:00:00Z",))

    def test_claimed_instants_type_fails_before_any_authority_call(self) -> None:
        current = Mock()
        post = Mock()
        horizon = Mock()
        guard = Mock()
        verifier = chronology._build_current_cut_with_horizon(
            require_current_cut=current,
            require_post_currentness=post,
            require_horizon=horizon,
            require_impl_namespace_sealed=guard,
        )

        with self.assertRaisesRegex(TypeError, "claimed_instants must be exact tuple"):
            verifier(claimed_instants=["2026-10-03T20:00:00Z"])

        guard.assert_not_called()
        current.assert_not_called()
        post.assert_not_called()
        horizon.assert_not_called()


if __name__ == "__main__":
    unittest.main()
