"""Plan 1 / Section 12: repository-owned provider-free financial qualification.

This orchestration is NOT a second financial, risk, OMS or scientific authority.
It executes existing suites against a single checked-out source tree. An AST
inventory check is not test execution or evidence of economic profitability.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

CAMPAIGNS: dict[str, tuple[str, ...]] = {
    "core": (
        "mvp.tests.test_economic_oracle_exactness",
        "mvp.tests.test_risk_exact_arithmetic",
        "mvp.tests.test_risk_policy_authority",
        "mvp.tests.test_durable_settlement",
        "mvp.tests.test_durable_financing_restart_conservation",
        "mvp.tests.test_terminal_fill_bust_reservation_conservation",
        "mvp.tests.test_atomic_oms_financial_commit",
        "mvp.tests.test_oms_exact_lifecycle",
    ),
    "portfolio": (
        "mvp.tests.test_allocation_evidence",
        "mvp.tests.test_allocation_payload_provenance",
        "mvp.tests.test_allocation_valuation",
        "mvp.tests.test_authority_allocation_snapshot_ingress",
        "mvp.tests.test_portfolio_correlation",
        "mvp.tests.test_portfolio_correlation_allocation_integrity",
        "mvp.tests.test_portfolio_correlation_oms",
        "mvp.tests.test_portfolio_correlation_resolver_ingress",
    ),
    "economics": (
        "mvp.tests.test_lifecycle_cost",
        "mvp.tests.test_lifecycle_cost_completeness",
        "mvp.tests.test_lifecycle_cost_allocation_integration",
        "mvp.tests.test_lifecycle_cost_instrument_identity",
        "mvp.tests.test_lifecycle_cost_valuation",
        "mvp.tests.test_zero_model_economics",
    ),
    "replay": (
        "mvp.tests.test_simulation_runtime_checkpoint",
        "mvp.tests.test_recovery_durable_unknown_restart",
        "mvp.tests.test_backup_restore",
        "mvp.tests.test_late_cancelled_fill_bust_conservation",
    ),
    "peer-contracts": (
        "mvp.tests.test_plan3_stack_qualification",
        "mvp.tests.test_scientific_financial_cut",
    ),
    "science": (
        "tests.Science.test_strategy_economics_authority",
        "tests.Science.test_strategy_economics_registered_run_authority",
    ),
}

REQUIRED_NEGATIVES: dict[str, tuple[str, ...]] = {
    "mvp.tests.test_economic_oracle_exactness": (
        "test_cash_round_trip_rejects_binary_float_money",
    ),
    "mvp.tests.test_durable_financing_restart_conservation": (
        "test_restart_rejects_duplicate_economics_for_one_financing_cause",
    ),
    "mvp.tests.test_atomic_oms_financial_commit": (
        "test_ack_loss_retry_is_exactly_once",
        "test_finance_only_split_fails_closed",
    ),
    "mvp.tests.test_allocation_payload_provenance": (
        "test_direct_hostile_mapping_is_rejected_before_callbacks",
    ),
    "mvp.tests.test_portfolio_correlation_oms": (
        "test_open_durable_order_blocks_otherwise_passing_correlation_admission",
    ),
    "mvp.tests.test_lifecycle_cost_allocation_integration": (
        "test_rebate_evidence_cannot_reduce_allocator_decision_cost",
        "test_lifecycle_cost_digest_reaches_science_but_cannot_self_issue_pass",
    ),
    "mvp.tests.test_zero_model_economics": (
        "test_remote_outage_degrades_to_no_model_without_exception",
    ),
    "mvp.tests.test_simulation_runtime_checkpoint": (
        "test_tampered_checkpoint_fails_before_provider_restore_or_journal_mutation",
        "test_crash_after_durable_completion_before_checkpoint_recovers_exactly",
    ),
    "mvp.tests.test_recovery_durable_unknown_restart": (
        "test_restart_rebuilds_durable_unknown_before_ready",
    ),
    "mvp.tests.test_backup_restore": (
        "test_restore_completion_rejects_unknown_reconciliation",
    ),
    "mvp.tests.test_plan3_stack_qualification": (
        "test_ambiguous_send_is_never_a_blind_retry_or_a_fill",
        "test_cross_plan_qualification_never_reinterprets_ci_as_scientific_pass",
    ),
    "mvp.tests.test_scientific_financial_cut": (
        "test_missing_current_reconciliation_is_unavailable_not_financial_pass",
    ),
    "tests.Science.test_strategy_economics_authority": (
        "test_fake_hashes_and_favorable_numbers_do_not_mint_authority",
    ),
}


def path_for(root: Path, module: str) -> Path:
    if not module or any(not part.isidentifier() for part in module.split(".")):
        raise ValueError("invalid qualification module name")
    return root.joinpath(*module.split(".")).with_suffix(".py")


def validate_inventory(
    root: Path = ROOT,
    campaigns: dict[str, tuple[str, ...]] = CAMPAIGNS,
    negatives: dict[str, tuple[str, ...]] = REQUIRED_NEGATIVES,
) -> dict[str, int]:
    if not campaigns:
        raise ValueError("no qualification campaigns")
    listed = {module for modules in campaigns.values() for module in modules}
    if not set(negatives).issubset(listed):
        raise ValueError("negative coverage names an unexecuted suite")
    counts: dict[str, int] = {}
    for name, modules in campaigns.items():
        if not modules:
            raise ValueError(f"empty qualification campaign: {name}")
        counts[name] = 0
        for module in modules:
            path = path_for(root, module)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"missing or symlinked suite: {module}")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            functions = {
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            }
            if not functions:
                raise ValueError(f"empty qualification suite: {module}")
            missing = set(negatives.get(module, ())) - functions
            if missing:
                raise ValueError(f"lost negative coverage: {module}: {sorted(missing)}")
            counts[name] += len(functions)
    return counts


def command_for(name: str, modules: tuple[str, ...]) -> list[str]:
    if name == "portfolio":
        return [sys.executable, "-m", "pytest", "-q", "-ra",
                *(module.replace(".", "/") + ".py" for module in modules)]
    if name == "science":
        return [sys.executable, "-m", "unittest", "discover", "-s",
                "tests/Science", "-p", "test_strategy_economics*.py", "-v"]
    return [sys.executable, "-m", "unittest", "-v", *modules]


def main() -> int:
    parser = argparse.ArgumentParser(description="Provider-free Plan-1 financial campaigns")
    parser.add_argument("--suite", choices=("all", *CAMPAIGNS), default="all")
    parser.add_argument("--check", action="store_true", help="AST inventory only, not test PASS")
    args = parser.parse_args()
    try:
        counts = validate_inventory()
    except (ValueError, OSError, UnicodeError, SyntaxError) as exc:
        print(f"PLAN1_QUALIFICATION_INVENTORY_FAIL: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "SOURCE_INVENTORY_ONLY", "tests_declared": counts},
                     sort_keys=True), flush=True)
    if args.check:
        return 0
    selected = tuple(CAMPAIGNS) if args.suite == "all" else (args.suite,)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(ROOT), str(ROOT / "research"), existing)))
    for name in selected:
        command = command_for(name, CAMPAIGNS[name])
        rc = subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
        if rc != 0:
            print(f"PLAN1_QUALIFICATION_FAIL: {name} rc={rc}", file=sys.stderr)
            return 1
        print(f"PLAN1_CAMPAIGN_PASS: {name}", flush=True)
    print("PLAN1_FINANCIAL_CAMPAIGNS_PASS (provider-free component tests only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
