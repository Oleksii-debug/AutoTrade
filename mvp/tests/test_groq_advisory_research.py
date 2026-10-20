from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.groq_advisory_research import (
    ADVISORY_ROLES,
    DurableGroqAdvisoryWave,
    advisory_result_json,
)
from mvp.autotrade_mvp.model_call import ModelCallObservation


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _observation(binding, text: str, *, prompt_tokens: int = 20):
    return ModelCallObservation(
        provider_id="groq",
        model_id=binding.model_id,
        revision=binding.revision,
        observed_at="2026-10-20T12:00:00Z",
        incurred_cost=Decimal(0),
        estimated_unbilled=Decimal(0),
        output={
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 12,
        },
        provider_response_id="response-" + binding.attempt_id[-12:],
        usage_id="usage-" + binding.attempt_id[-12:],
    )


def _role_calls(calls: list[tuple[str, str, str]], output=None):
    result = {}
    for role in ADVISORY_ROLES:
        def call(binding, _cancelled, selected=role):
            calls.append((selected.role, selected.profile, binding.model_id))
            text = (
                output(selected) if output is not None
                else f"{selected.role}: hypothesis requires causal numeric rejection tests"
            )
            return _observation(binding, text)
        result[role.role] = call
    return result


class DurableGroqAdvisoryWaveTests(unittest.TestCase):
    def test_runs_fixed_four_role_wave_and_persists_sqlite_jsonl(self):
        with TemporaryDirectory() as root:
            calls = []
            runner = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            )
            result = runner.run(
                wave_id="wave-001",
                research_input={
                    "evidence_class": "PUBLIC_MARKET_ZERO",
                    "independent_net_usdt": "-3.2505",
                    "profitability_proven": False,
                    "observations": 1486,
                },
            )

            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(
                calls,
                [(item.role, item.profile, item.model_id) for item in ADVISORY_ROLES],
            )
            self.assertEqual([item.status for item in result.episodes], ["OBSERVED"] * 4)
            self.assertTrue(Path(result.database_path).is_file())
            self.assertTrue(Path(result.jsonl_path).is_file())
            events = [
                json.loads(line)
                for line in Path(result.jsonl_path).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event_type"], "AdvisoryWaveCompleted")
            self.assertEqual(events[-1]["status"], "COMPLETED")
            prepared = next(
                item for item in events
                if item["event_type"] == "AdvisoryEpisodePrepared"
            )
            observed = next(
                item for item in events
                if item["event_type"] == "AdvisoryEpisodeObserved"
            )
            self.assertIn("RESEARCH-ONLY", prepared["details"]["prompt"])
            self.assertEqual(observed["details"]["prompt_tokens"], 20)
            self.assertIn("hypothesis", observed["details"]["output_text"])
            self.assertTrue((Path(root) / "groq-quota.sqlite3").is_file())
            with closing(sqlite3.connect(Path(root) / "groq-quota.sqlite3")) as connection:
                quota_rows = connection.execute(
                    "SELECT profile,model_id FROM quota_reservations ORDER BY created_at,rowid"
                ).fetchall()
            self.assertEqual(
                quota_rows,
                [(item.profile, item.model_id) for item in ADVISORY_ROLES],
            )

            # Completed waves are restart-idempotent and consume no second call.
            restarted = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            ).run(
                wave_id="wave-001",
                research_input={
                    "evidence_class": "PUBLIC_MARKET_ZERO",
                    "independent_net_usdt": "-3.2505",
                    "profitability_proven": False,
                    "observations": 1486,
                },
            )
            self.assertEqual(restarted.status, "COMPLETED")
            self.assertEqual(len(calls), 4)

    def test_failed_started_call_is_unknown_and_never_blindly_retried(self):
        with TemporaryDirectory() as root:
            calls = []
            role_calls = _role_calls(calls)

            def fail(binding, _cancelled):
                calls.append(("coordinator", "groq-account-1", binding.model_id))
                raise OSError("provider response was lost")

            role_calls["coordinator"] = fail
            first = DurableGroqAdvisoryWave(
                root, calls_by_role=role_calls, clock=lambda: NOW,
            ).run(wave_id="wave-unknown", research_input={"metric": "1"})
            self.assertEqual(first.status, "UNKNOWN")
            self.assertEqual(first.episodes[0].status, "UNKNOWN")
            self.assertEqual(len(calls), 1)

            second = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            ).run(wave_id="wave-unknown", research_input={"metric": "1"})
            self.assertEqual(second.status, "UNKNOWN")
            self.assertEqual(len(calls), 1)

            with closing(sqlite3.connect(second.database_path)) as connection:
                error_code = connection.execute(
                    "SELECT error_code FROM advisory_episodes WHERE wave_id=?",
                    ("wave-unknown",),
                ).fetchone()[0]
            self.assertEqual(error_code, "CALL_OUTCOME_UNKNOWN")

    def test_prepared_but_unsent_episode_resumes_after_restart(self):
        with TemporaryDirectory() as root:
            calls = []
            runner = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            )
            input_json = json.dumps(
                {"metric": "1"}, sort_keys=True, separators=(",", ":")
            )
            created_at = runner._ensure_wave("wave-prepared", input_json)
            role = ADVISORY_ROLES[0]
            prompt = runner._prompt(role, input_json, ())
            binding = runner._binding(
                role=role,
                wave_id="wave-prepared",
                prompt=prompt,
                created_at=created_at,
            )
            self.assertEqual(
                runner._prepare_episode(
                    wave_id="wave-prepared",
                    role=role,
                    prompt=prompt,
                    binding=binding,
                ),
                "PREPARED",
            )

            result = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            ).run(
                wave_id="wave-prepared", research_input={"metric": "1"}
            )
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(len(calls), 4)

    def test_restart_recovers_started_episode_as_unknown_without_sending(self):
        with TemporaryDirectory() as root:
            calls = []
            runner = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            )
            input_json = json.dumps(
                {"metric": "1"}, sort_keys=True, separators=(",", ":")
            )
            created_at = runner._ensure_wave("wave-interrupted", input_json)
            role = ADVISORY_ROLES[0]
            prompt = runner._prompt(role, input_json, ())
            binding = runner._binding(
                role=role,
                wave_id="wave-interrupted",
                prompt=prompt,
                created_at=created_at,
            )
            runner._prepare_episode(
                wave_id="wave-interrupted",
                role=role,
                prompt=prompt,
                binding=binding,
            )
            runner._mark_started("wave-interrupted", role)

            result = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            ).run(
                wave_id="wave-interrupted", research_input={"metric": "1"}
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.episodes[0].status, "UNKNOWN")
            self.assertEqual(calls, [])

    def test_secret_like_input_is_rejected_before_episode_persistence(self):
        with TemporaryDirectory() as root:
            calls = []
            runner = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            )
            with self.assertRaisesRegex(ValueError, "secret-like"):
                runner.run(
                    wave_id="wave-secret-input",
                    research_input={"api_key": "gsk_secret-value-123456"},
                )
            self.assertEqual(calls, [])
            with closing(sqlite3.connect(runner.database_path)) as connection:
                count = connection.execute("SELECT COUNT(*) FROM advisory_waves").fetchone()[0]
            self.assertEqual(count, 0)

    def test_secret_like_model_text_is_redacted_from_all_durable_records(self):
        with TemporaryDirectory() as root:
            calls = []
            runner = DurableGroqAdvisoryWave(
                root,
                calls_by_role=_role_calls(
                    calls,
                    output=lambda role: (
                        f"{role.role} accidentally emitted gsk_ABCDEF1234567890"
                    ),
                ),
                clock=lambda: NOW,
            )
            result = runner.run(
                wave_id="wave-secret-output", research_input={"metric": "1"},
            )
            self.assertEqual(result.status, "COMPLETED")
            self.assertTrue(all("[REDACTED]" in item.output_text for item in result.episodes))
            for path in Path(root).glob("*"):
                if path.is_file():
                    self.assertNotIn(b"gsk_", path.read_bytes(), path.name)

    def test_prompts_enforce_research_only_boundary_and_cli_projection_is_safe(self):
        with TemporaryDirectory() as root:
            calls = []
            result = DurableGroqAdvisoryWave(
                root, calls_by_role=_role_calls(calls), clock=lambda: NOW,
            ).run(wave_id="wave-boundary", research_input={"mode": "RESEARCH_ONLY"})
            with closing(sqlite3.connect(result.database_path)) as connection:
                prompts = [row[0] for row in connection.execute(
                    "SELECT prompt FROM advisory_episodes ORDER BY ordinal"
                )]
            self.assertEqual(len(prompts), 4)
            self.assertTrue(all("no trading, order" in prompt for prompt in prompts))
            self.assertTrue(all("Never issue BUY/SELL" in prompt for prompt in prompts))
            projected = json.loads(advisory_result_json(result))
            self.assertEqual(projected["status"], "COMPLETED")
            self.assertEqual(len(projected["episodes"]), 4)


if __name__ == "__main__":
    unittest.main()
