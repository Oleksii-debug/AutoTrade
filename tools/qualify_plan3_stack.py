"""Plan-3 Section-8 *qualification runner*, not a runtime or financial authority.

Runs existing repository-owned recovery, backup, Host and operator suites as one
exact-checkout campaign. A failed or absent suite is never silently skipped.
No provider credentials, external services or real money are required.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# The single existing implementations remain in mvp/autotrade_mvp/.
# These modules contain crash/restart, split-brain, recovery, restore, journal,
# immutable sender-fence and redacted operator negative tests.
CAMPAIGNS: dict[str, tuple[str, ...]] = {
    "recovery": (
        "mvp.tests.test_plan3_stack_qualification",
        "mvp.tests.test_recovery_takeover",
        "mvp.tests.test_recovery_takeover_source_sender_fence",
        "mvp.tests.test_recovery_takeover_stale_sender",
        "mvp.tests.test_recovery_durable_unknown_restart",
        "mvp.tests.test_runtime_failure_control",
    ),
    "backup": (
        "mvp.tests.test_backup_restore",
        "mvp.tests.test_simulation_runtime_checkpoint",
    ),
    "host": (
        "mvp.tests.test_production_host",
        "mvp.tests.test_production_host_config",
        "mvp.tests.test_production_host_readiness",
        "mvp.tests.test_production_host_runtime_occurrence",
        "mvp.tests.test_production_financial_host",
        "mvp.tests.test_production_financial_host_authority_bindings",
        "mvp.tests.test_host_network",
        "mvp.tests.test_host_api",
        "mvp.tests.test_durable_host_api",
        "mvp.tests.test_readiness",
    ),
    "observability": (
        "mvp.tests.test_operator_observability",
        "mvp.tests.test_diagnostics",
        "mvp.tests.test_decision_trace",
    ),
}

# Positive coverage cannot substitute for these named adversarial assertions.
# Fail closed if a regression silently deletes or renames the negative oracle.
REQUIRED_NEGATIVES: dict[str, tuple[str, ...]] = {
    "mvp.tests.test_plan3_stack_qualification": (
        "test_recovery_restart_reconciliation_and_secret_safe_diagnostic_cut",
        "test_ambiguous_send_is_never_a_blind_retry_or_a_fill",
        "test_legacy_backup_restore_preserves_diagnostics_and_blocks_authority",
    ),
    "mvp.tests.test_recovery_durable_unknown_restart": (
        "test_restart_rebuilds_durable_unknown_before_ready",
        "test_reconciliation_identity_mismatch_cannot_clear_recovered_unknown",
    ),
    "mvp.tests.test_recovery_takeover_stale_sender": (
        "test_completed_takeover_reopens_gate_but_old_shared_journal_sender_emits_zero_bytes",
    ),
    "mvp.tests.test_backup_restore": (
        "test_late_order_intent_during_journal_snapshot_blocks_publication",
        "test_restore_completion_rejects_unknown_reconciliation",
    ),
    "mvp.tests.test_simulation_runtime_checkpoint": (
        "test_tampered_checkpoint_fails_before_provider_restore_or_journal_mutation",
        "test_crash_after_durable_completion_before_checkpoint_recovers_exactly",
    ),
    "mvp.tests.test_production_host_runtime_occurrence": (
        "test_runtime_occurrence_is_durable_unique_and_restart_ordered",
        "test_newer_durable_occurrence_invalidates_bound_runtime_property",
    ),
    "mvp.tests.test_operator_observability": (
        "test_unknown_sends_and_uncertain_external_state_cannot_report_ready",
        "test_original_private_payloads_never_enter_accessible_text_or_json",
        "test_concurrent_recovery_change_rejects_torn_diagnostic_cut",
        "test_durable_ready_projection_rejects_foreign_account_and_host",
    ),
}


def validate_inventory(root: Path = ROOT) -> dict[str, int]:
    counts: dict[str, int] = {}
    for campaign, modules in CAMPAIGNS.items():
        counts[campaign] = 0
        for module in modules:
            file = root.joinpath(*module.split(".")).with_suffix(".py")
            if not file.is_file() or file.is_symlink():
                raise RuntimeError(f"Missing or symlinked qualification suite: {module}")
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
            funcs = {
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            }
            if not funcs:
                raise RuntimeError(f"Empty qualification suite: {module}")
            missing = set(REQUIRED_NEGATIVES.get(module, ())) - funcs
            if missing:
                raise RuntimeError(f"Missing negative assertions in {module}: {sorted(missing)}")
            counts[campaign] += len(funcs)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan 3 / Section 8 offline whole-stack campaigns")
    parser.add_argument("--suite", choices=("all", *CAMPAIGNS), default="all")
    parser.add_argument("--check", action="store_true",
                        help="Source inventory only; NOT test PASS or terminal DONE")
    args = parser.parse_args()

    try:
        counts = validate_inventory()
    except (OSError, UnicodeError, SyntaxError, RuntimeError) as exc:
        print(f"PLAN3_QUALIFICATION_INVENTORY_FAIL: {exc}", file=sys.stderr)
        return 2

    print(json.dumps({"source_inventory": counts,
                      "state": "SOURCE_INVENTORY_ONLY"}, sort_keys=True), flush=True)
    if args.check:
        return 0

    selected = tuple(CAMPAIGNS) if args.suite == "all" else (args.suite,)
    for name in selected:
        # A fresh test process per domain prevents accidental cross-suite
        # global-state reuse; all processes read the same checked-out tree.
        cmd = [sys.executable, "-m", "unittest", "-v", *CAMPAIGNS[name]]
        result = subprocess.run(cmd, cwd=ROOT, check=False)
        if result.returncode != 0:
            print(f"PLAN3_QUALIFICATION_FAIL: {name} rc={result.returncode}",
                  file=sys.stderr)
            return 1
        print(f"PLAN3_CAMPAIGN_PASS: {name}", flush=True)
    print("PLAN3_PYTHON_CAMPAIGNS_PASS (native Host.Process separately required)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
