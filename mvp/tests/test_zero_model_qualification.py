from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
import os
import socket
from pathlib import Path
import platform
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qualification.zero_model.qualify import (
    UnavailableModelInventory,
    _network_denied,
    _observed_source_sha,
    _qualifier_sha256,
    _require_clean_checkout,
    _runtime_qualifier_bytes,
    _require_exact_checkout,
    _require_source_sha,
    _trusted_git_environment,
    qualify,
)


ROOT = Path(__file__).resolve().parents[2]


class ZeroModelQualificationTests(unittest.TestCase):
    def test_qualification_workflow_runs_on_windows_and_ubuntu_with_distinct_evidence(self):
        workflow = (
            ROOT / ".github" / "workflows" / "zero-model-qualification.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("os: [ubuntu-22.04, windows-2025]", workflow)
        self.assertIn("runs-on: ${{ matrix.os }}", workflow)
        self.assertIn(
            "zero-model-qualification-${{ runner.os }}-${{ env.EXPECTED_SOURCE_SHA }}",
            workflow,
        )
        self.assertIn('"mvp/autotrade_mvp/**"', workflow)
        self.assertIn('"research/autotrade_research/**"', workflow)
        self.assertIn("mvp.tests.test_zero_model_economics", workflow)

    def test_zero_model_slice_is_replayable_reconciled_and_cost_free(self):
        observed = _observed_source_sha()
        with patch(
            "qualification.zero_model.qualify._require_exact_checkout",
            wraps=_require_exact_checkout,
        ) as exact_checkout:
            evidence = qualify(observed)

        self.assertEqual(exact_checkout.call_count, 2)
        self.assertEqual(
            [entry.args for entry in exact_checkout.call_args_list],
            [(observed,), (observed,)],
        )
        self.assertEqual(evidence["qualification"], "WP-62_ZERO_MODEL_FOUNDATION")
        self.assertEqual(
            evidence["execution_platform"],
            {
                "system": platform.system(),
                "python_implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
            },
        )
        self.assertEqual(evidence["source_sha"], observed)
        self.assertEqual(evidence["observed_source_sha"], observed)
        self.assertTrue(evidence["source_checkout_clean"])
        self.assertRegex(evidence["qualifier_sha256"], r"^sha256:[0-9a-f]{64}$")
        route = evidence["model_route"]
        self.assertEqual(route["status"], "NO_MODEL")
        self.assertIsNone(route["model_id"])
        self.assertIsNone(route["provider_id"])
        self.assertEqual(route["reserved_cost"], "0")
        self.assertFalse(route["model_inventory_touched"])

        outages = evidence["outage_routes"]
        self.assertEqual(set(outages), {"remote_outage", "local_resource_exhaustion"})
        for outage in outages.values():
            self.assertEqual(outage["status"], "NO_MODEL")
            self.assertIsNone(outage["model_id"])
            self.assertIsNone(outage["provider_id"])
            self.assertEqual(outage["reserved_cost"], "0")
            self.assertEqual(outage["reason"], "no_admissible_model")
        self.assertEqual(evidence["model_cost_total"], "0")
        self.assertEqual(
            evidence["network_guard"],
            {
                "python_socket_io_blocked": True,
                "network_attempt_count": 0,
            },
        )

        autonomous = evidence["canonical_autonomous_zero_loop"]
        self.assertEqual(autonomous["continuous_status"], "COMPLETED")
        self.assertEqual(autonomous["paused_status"], "PAUSED")
        self.assertEqual(autonomous["resumed_status"], "COMPLETED")
        self.assertEqual(autonomous["replay_status"], "COMPLETED")
        self.assertEqual(autonomous["mode"], "ZERO")
        self.assertEqual(autonomous["episodes"], 120)
        self.assertEqual(autonomous["pause_cut"], 41)
        self.assertGreater(autonomous["continuous_outbound_requests"], 20)
        self.assertEqual(
            autonomous["pause_resume_outbound_requests"],
            autonomous["continuous_outbound_requests"],
        )
        self.assertEqual(autonomous["replay_outbound_requests"], 0)
        self.assertTrue(autonomous["same_decisions_after_resume"])
        self.assertTrue(autonomous["same_economics_after_resume"])
        self.assertEqual(autonomous["economic_edge_status"], "INCONCLUSIVE")

        unknown = evidence["canonical_unknown_no_resend"]
        self.assertEqual(unknown["initial_status"], "UNKNOWN")
        self.assertEqual(unknown["reentry_status"], "UNKNOWN")
        self.assertEqual(unknown["initial_outbound_requests"], 1)
        self.assertEqual(unknown["reentry_outbound_requests"], 0)

        emergency = evidence["canonical_emergency_zero_loop"]
        self.assertEqual(emergency["status"], "COMPLETED")
        self.assertEqual(emergency["episodes"], 8)
        self.assertEqual(emergency["outbound_requests"], 1)
        self.assertEqual(emergency["replay_outbound_requests"], 0)
        self.assertEqual(emergency["economic_edge_status"], "INCONCLUSIVE")

        partial = evidence["canonical_partial_fill_zero_loop"]
        self.assertEqual(partial["status"], "COMPLETED")
        self.assertEqual(partial["mode"], "ZERO")
        self.assertEqual(partial["execution_profile"], "TWO_EQUAL_PARTIALS")
        self.assertGreater(partial["new_outbound_requests"], 0)
        self.assertEqual(partial["replay_outbound_requests"], 0)
        self.assertTrue(partial["same_decisions_after_replay"])
        self.assertEqual(partial["economic_edge_status"], "INCONCLUSIVE")

        financial = evidence["deterministic_financial_slice"]
        self.assertTrue(financial["resumed"])
        self.assertTrue(financial["same_order_identity"])
        self.assertTrue(financial["same_fill_identity"])
        self.assertTrue(financial["reconciled"])
        self.assertTrue(financial["replay_verified"])

        economics = evidence["economics"]
        self.assertEqual(economics["economic_edge_claim"], "UNPROVEN_SIMULATION_ONLY")
        self.assertEqual(economics["evidence_count"], 1)
        self.assertEqual(economics["trade_count"], 1)
        self.assertGreaterEqual(Decimal(economics["total_fees"]), Decimal("0"))

        campaign = evidence["multi_episode_economics"]
        self.assertEqual(campaign["episode_statuses"], ["filled", "filled"])
        self.assertEqual(campaign["replay_statuses"], ["filled", "filled"])
        self.assertTrue(campaign["restart_resumed"])
        self.assertTrue(campaign["same_order_identities"])
        self.assertTrue(campaign["same_fill_identities"])
        self.assertTrue(campaign["reconciled"])
        campaign_economics = campaign["economics"]
        self.assertEqual(campaign_economics["trade_count"], 2)
        self.assertEqual(campaign_economics["evidence_count"], 2)
        self.assertEqual(Decimal(campaign_economics["ending_position"]), Decimal("0"))
        self.assertEqual(
            campaign_economics["economic_edge_claim"],
            "UNPROVEN_SIMULATION_ONLY",
        )
        self.assertGreater(Decimal(campaign_economics["total_fees"]), Decimal("0"))
        self.assertLess(Decimal(campaign_economics["net_pnl"]), Decimal("0"))

        small = evidence["small_capital"]
        self.assertEqual(small["status"], "risk_rejected")
        self.assertTrue(small["resumed"])
        self.assertIsNone(small["order_id"])
        self.assertIsNone(small["fill_id"])
        self.assertTrue(small["reconciled"])
        self.assertTrue(small["replay_verified"])
        self.assertEqual(small["economics"]["trade_count"], 0)
        self.assertEqual(small["economics"]["net_pnl"], "0")

        claims = evidence["claims"]
        self.assertFalse(claims["network_or_model_call_performed"])
        self.assertFalse(claims["live_trading_qualified"])
        self.assertFalse(claims["economic_edge_proven"])
        self.assertFalse(claims["all_wp62_workflows_qualified"])

    def test_zero_inventory_sentinel_rejects_all_container_inspection(self):
        for name, inspect in (
            ("iter", lambda inventory: iter(inventory)),
            ("len", lambda inventory: len(inventory)),
            ("bool", lambda inventory: bool(inventory)),
            ("index", lambda inventory: inventory[0]),
            ("contains", lambda inventory: "remote" in inventory),
        ):
            with self.subTest(operation=name):
                inventory = UnavailableModelInventory()
                with self.assertRaisesRegex(
                    RuntimeError, "must not inspect unavailable model inventory"
                ):
                    inspect(inventory)
                self.assertTrue(inventory.touched)

    def test_qualification_denies_python_network_access(self):
        observed = _observed_source_sha()

        def forbidden_network_slice(*args, **kwargs):
            del args, kwargs
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)

        with patch(
            "qualification.zero_model.qualify.run_vertical_slice",
            side_effect=forbidden_network_slice,
        ):
            with self.assertRaisesRegex(RuntimeError, "attempted network access"):
                qualify(observed)

    def test_network_decorator_does_not_follow_rebound_guard_alias(self):
        calls = []

        def harmless_connect(*args, **kwargs):
            calls.append((args, kwargs))
            return object()

        @contextmanager
        def forged_guard():
            yield []

        @_network_denied
        def probe():
            socket.create_connection(("example.invalid", 443))
            return {"claims": {}}

        with (
            patch(
                "qualification.zero_model.qualify._deny_network_access",
                forged_guard,
            ),
            patch.object(socket, "create_connection", harmless_connect),
        ):
            with self.assertRaisesRegex(RuntimeError, "attempted network access"):
                probe()

        self.assertEqual(calls, [])

    def test_network_decorator_blocks_new_socket_construction(self):
        @_network_denied
        def probe():
            socket.socket()
            return {"claims": {}}

        with self.assertRaisesRegex(RuntimeError, "attempted network access"):
            probe()

    def test_network_decorator_blocks_socketpair_construction(self):
        if not hasattr(socket, "socketpair"):
            self.skipTest("socketpair unavailable on this platform")

        @_network_denied
        def probe():
            socket.socketpair()
            return {"claims": {}}

        with self.assertRaisesRegex(RuntimeError, "attempted network access"):
            probe()

    def test_network_decorator_blocks_send_on_preexisting_socket(self):
        calls = []

        def harmless_send(_self, data, *args, **kwargs):
            calls.append((data, args, kwargs))
            return len(data)

        @_network_denied
        def probe(sock):
            sock.send(b"forbidden")
            return {"claims": {}}

        sock = socket.socket()
        try:
            with patch.object(socket.socket, "send", harmless_send):
                with self.assertRaisesRegex(RuntimeError, "attempted network access"):
                    probe(sock)
        finally:
            sock.close()

        self.assertEqual(calls, [])

    def test_expected_source_identity_must_match_actual_checkout(self):
        observed = _observed_source_sha()
        different = ("0" if observed[0] != "0" else "1") + observed[1:]
        with self.assertRaisesRegex(RuntimeError, "actual Git checkout"):
            _require_exact_checkout(different)
        self.assertEqual(_require_exact_checkout(observed), observed)

    def test_qualifier_digest_is_bound_to_exact_source_blob(self):
        source_sha = "a" * 40
        canonical_blob = b"canonical zero-model qualifier bytes\n"
        with (
            patch(
                "qualification.zero_model.qualify._git_bytes",
                return_value=SimpleNamespace(stdout=canonical_blob),
            ) as git,
            patch(
                "qualification.zero_model.qualify._runtime_qualifier_bytes",
                return_value=canonical_blob,
            ) as runtime,
        ):
            digest = _qualifier_sha256(source_sha)

        self.assertEqual(digest, "sha256:" + sha256(canonical_blob).hexdigest())
        self.assertEqual(
            git.call_args.args,
            (
                "cat-file",
                "blob",
                f"{source_sha}:qualification/zero_model/qualify.py",
            ),
        )
        runtime.assert_called_once_with()

    def test_runtime_qualifier_bytes_must_match_exact_source_blob(self):
        source_sha = "a" * 40
        with (
            patch(
                "qualification.zero_model.qualify._git_bytes",
                return_value=SimpleNamespace(stdout=b"git-source\n"),
            ),
            patch(
                "qualification.zero_model.qualify._runtime_qualifier_bytes",
                return_value=b"runtime-source\n",
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime qualifier bytes do not match exact Git source blob",
            ):
                _qualifier_sha256(source_sha)

    def test_runtime_qualifier_source_is_canonical_checkout_path(self):
        self.assertEqual(
            _runtime_qualifier_bytes(),
            (
                ROOT / "qualification" / "zero_model" / "qualify.py"
            ).read_bytes(),
        )

    def test_source_identity_is_read_from_exact_qualifier_checkout_root(self):
        expected = "a" * 40
        with patch(
            "qualification.zero_model.qualify._git",
            side_effect=(
                SimpleNamespace(stdout=str(ROOT) + "\n"),
                SimpleNamespace(stdout=expected + "\n"),
            ),
        ) as git:
            self.assertEqual(_observed_source_sha(), expected)
        self.assertEqual(git.call_args_list[0].args, ("rev-parse", "--show-toplevel"))
        self.assertEqual(git.call_args_list[1].args, ("rev-parse", "HEAD"))

    def test_dirty_checkout_cannot_issue_zero_model_qualification(self):
        with patch(
            "qualification.zero_model.qualify._git",
            return_value=SimpleNamespace(
                stdout=" M mvp/autotrade_mvp/simulation_session.py\n"
            ),
        ) as git:
            with self.assertRaisesRegex(RuntimeError, "source changes"):
                _require_clean_checkout()
        self.assertEqual(
            git.call_args.args,
            (
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
        )

    def test_git_inspection_environment_drops_caller_repository_authority(self):
        poisoned = {
            "PATH": str(ROOT / "attacker-bin"),
            "GIT_DIR": str(ROOT / "attacker.git"),
            "GIT_WORK_TREE": str(ROOT / "attacker-worktree"),
            "GIT_CONFIG_GLOBAL": str(ROOT / "attacker.gitconfig"),
        }
        with patch.dict(os.environ, poisoned, clear=False):
            environment = _trusted_git_environment()

        self.assertNotIn("PATH", environment)
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_WORK_TREE", environment)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")

    def test_source_sha_rejects_text_subclasses_before_observation(self):
        class HostileSha(str):
            pass

        with self.assertRaisesRegex(TypeError, "exact text"):
            _require_source_sha(HostileSha("a" * 40))

    def test_source_sha_accepts_canonical_sha1_or_sha256_only(self):
        self.assertEqual(_require_source_sha("a" * 40), "a" * 40)
        self.assertEqual(_require_source_sha("b" * 64), "b" * 64)

        for invalid in (
            "abc",
            "g" * 40,
            "a" * 39,
            "a" * 41,
            "a" * 63,
            "a" * 65,
            "A" * 40,
            "B" * 64,
            " " + "a" * 40,
            "a" * 64 + " ",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _require_source_sha(invalid)


if __name__ == "__main__":
    unittest.main()
