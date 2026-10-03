from decimal import Decimal
import os
from pathlib import Path
import platform
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qualification.zero_model.qualify import (
    _git,
    _observed_source_sha,
    _require_clean_checkout,
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
        self.assertIn("os: [ubuntu-latest, windows-latest]", workflow)
        self.assertIn("runs-on: ${{ matrix.os }}", workflow)
        self.assertIn(
            "zero-model-qualification-${{ runner.os }}-${{ env.EXPECTED_SOURCE_SHA }}",
            workflow,
        )
        self.assertIn("mvp.tests.test_zero_model_economics", workflow)
        for path in (
            "mvp/autotrade_mvp/**",
            "research/autotrade_research/economics/**",
            "mvp/tests/test_zero_model_qualification.py",
            "mvp/tests/test_zero_model_economics.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(workflow.count(path), 2)

    def test_zero_model_slice_is_replayable_reconciled_and_cost_free(self):
        observed = _observed_source_sha()
        evidence = qualify(observed)

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

    def test_expected_source_identity_must_match_actual_checkout(self):
        observed = _observed_source_sha()
        different = ("0" if observed[0] != "0" else "1") + observed[1:]
        with self.assertRaisesRegex(RuntimeError, "actual Git checkout"):
            _require_exact_checkout(different)
        self.assertEqual(_require_exact_checkout(observed), observed)

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

    def test_parent_repository_cannot_supply_qualifier_source_identity(self):
        with patch(
            "qualification.zero_model.qualify._git",
            return_value=SimpleNamespace(stdout=str(ROOT.parent) + "\n"),
        ):
            with self.assertRaisesRegex(RuntimeError, "exact Git top-level"):
                _observed_source_sha()

    def test_git_inspection_drops_caller_repository_path_and_config_authority(self):
        completed = SimpleNamespace(stdout="a" * 40 + "\n")
        poisoned = {
            "PATH": str(ROOT / "attacker-bin"),
            "GIT_DIR": str(ROOT / "attacker.git"),
            "GIT_WORK_TREE": str(ROOT / "attacker-worktree"),
            "GIT_CONFIG_GLOBAL": str(ROOT / "attacker.gitconfig"),
        }
        with patch.dict(os.environ, poisoned, clear=False):
            with patch(
                "qualification.zero_model.qualify._trusted_git_executable",
                return_value="/usr/bin/git",
            ), patch(
                "qualification.zero_model.qualify.subprocess.run",
                return_value=completed,
            ) as run:
                self.assertIs(_git("rev-parse", "HEAD"), completed)

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command[0], "/usr/bin/git")
        self.assertEqual(command[1:], ["rev-parse", "HEAD"])
        self.assertEqual(run.call_args.kwargs["cwd"], ROOT)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertNotIn("PATH", environment)
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_WORK_TREE", environment)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")

    def test_trusted_git_environment_is_minimal(self):
        environment = _trusted_git_environment()
        self.assertNotIn("PATH", environment)
        for forbidden in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            self.assertNotIn(forbidden, environment)

    def test_dirty_checkout_cannot_issue_zero_model_qualification(self):
        with patch(
            "qualification.zero_model.qualify._git",
            return_value=SimpleNamespace(
                stdout=" M mvp/autotrade_mvp/pipeline.py\n"
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
