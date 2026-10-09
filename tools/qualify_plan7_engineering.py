"""Plan-7 Section-7 source-pinned test orchestration, NOT a new gate issuer.

Executes the existing Science, Plan-2 ablation, Plan-4 trust, strategy
economics, evidence matrix and runtime-load suites. It cannot grant economic
edge, model promotion, financial authority, provider/PAPER/LIVE or release PASS.
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

FIXED_CAMPAIGNS: dict[str, tuple[str, ...]] = {
    "science": ("tests.Science.test_science_qualification",),
    "ablation": (
        "research.tests.test_ablation",
        "research.tests.test_ablation_exact_rational",
        "tests.Science.test_ablation_component_qualification",
    ),
    "economics": (
        "tests.Science.test_strategy_economics_authority",
        "tests.Science.test_strategy_economics_registered_run_authority",
    ),
    "trust": (
        "mvp.tests.test_qualification_attestation",
        "mvp.tests.test_qualification_attestation_authority_ingress",
        "mvp.tests.test_release_qualification",
    ),
    "evidence-matrix": ("mvp.tests.test_evidence_class_matrix",),
    "performance": (
        "mvp.tests.test_performance_qualification",
        "mvp.tests.test_runtime_resource_budget",
    ),
}
DYNAMIC_CAMPAIGNS: dict[str, str] = {
    "performance": "test_runtime_load_*.py",
    "target-host": "test_runtime_target_host_*.py",
}
CRITICAL_NEGATIVES: dict[str, tuple[str, ...]] = {
    "tests.Science.test_science_qualification": (
        "test_caller_lambda_true_cannot_self_approve_science",
        "test_signed_failed_gate_cannot_be_relabelled_pass",
        "test_edge_claim_requires_forward_evidence",
    ),
    "tests.Science.test_ablation_component_qualification": (
        "test_signed_science_gate_cannot_be_self_issued_from_diagnostic_ablation",
        "test_restart_safe_durable_receipt_and_tamper_failure",
    ),
    "tests.Science.test_strategy_economics_authority": (
        "test_fake_hashes_and_favorable_numbers_do_not_mint_authority",
        "test_private_registry_cannot_reseal_mutated_positive_authority",
    ),
    "tests.Science.test_strategy_economics_registered_run_authority": (
        "test_favorable_fake_economics_remain_unresolved_after_real_run_replay",
    ),
    "mvp.tests.test_qualification_attestation": (
        "test_invalid_signature_is_rejected_before_reader_construction_callback",
        "test_verifier_rejects_evidence_resolver_rebind_during_evidence_read",
    ),
    "mvp.tests.test_evidence_class_matrix": (
        "test_self_issued_pass_is_explicitly_unverified",
        "test_stale_git_source_identity_is_not_replayed_as_current",
        "test_wrong_class_cannot_qualify_provider_paper_live_nvda_or_signed_release",
    ),
    "mvp.tests.test_performance_qualification": (
        "test_factory_evidence_without_event_identity_is_inconclusive",
    ),
    "mvp.tests.test_runtime_target_host_runner": (
        "test_executes_only_predeclared_work_and_retains_nonterminal_chain",
        "test_later_callback_code_mutation_is_rejected_before_financial_operation",
    ),
}


def module_path(root: Path, module: str) -> Path:
    if not module or any(not item.isidentifier() for item in module.split(".")):
        raise ValueError("noncanonical module path")
    return root.joinpath(*module.split(".")).with_suffix(".py")


def campaigns_for(root: Path = ROOT) -> dict[str, tuple[str, ...]]:
    result = dict(FIXED_CAMPAIGNS)
    for name, pattern in DYNAMIC_CAMPAIGNS.items():
        matches = tuple(
            f"mvp.tests.{file.stem}"
            for file in sorted((root / "mvp" / "tests").glob(pattern))
            if file.is_file() and not file.is_symlink()
        )
        if not matches:
            raise ValueError(f"required dynamic campaign missing: {name}")
        result[name] = result.get(name, ()) + matches
    return result


def validate_inventory(
    root: Path = ROOT,
    campaigns: dict[str, tuple[str, ...]] | None = None,
    negatives: dict[str, tuple[str, ...]] = CRITICAL_NEGATIVES,
) -> dict[str, int]:
    suites = campaigns if campaigns is not None else campaigns_for(root)
    if not suites or any(not group for group in suites.values()):
        raise ValueError("empty qualification campaign")
    all_modules = {module for group in suites.values() for module in group}
    if not set(negatives).issubset(all_modules):
        raise ValueError("critical negative suites not scheduled")
    counts: dict[str, int] = {}
    for name, modules in suites.items():
        if len(modules) != len(set(modules)):
            raise ValueError(f"duplicate suite: {name}")
        counts[name] = 0
        for module in modules:
            path = module_path(root, module)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"missing or symlinked suite: {module}")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = {
                node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            }
            if not names:
                raise ValueError(f"empty suite: {module}")
            missing = set(negatives.get(module, ())) - names
            if missing:
                raise ValueError(f"required negative disappeared: {module}: {sorted(missing)}")
            counts[name] += len(names)
    return counts


def verify_source_pin(root: Path = ROOT, expected: str | None = None) -> None:
    if not expected:
        return
    current = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    if current != expected:
        raise ValueError("qualification checkout/source SHA mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan-7 provider-free test campaign")
    parser.add_argument("--suite", default="all",
                        choices=("all", *FIXED_CAMPAIGNS, "target-host"))
    parser.add_argument("--check", action="store_true",
                        help="AST inventory only; must never claim tests passed")
    args = parser.parse_args()
    try:
        campaigns = campaigns_for(ROOT)
        counts = validate_inventory(ROOT, campaigns)
        verify_source_pin(ROOT, os.environ.get("AUTOTRADE_SOURCE_SHA"))
    except (ValueError, OSError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"PLAN7_SOURCE_INVENTORY_FAIL: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"state": "SOURCE_INVENTORY_ONLY",
                      "tests_declared_not_executed": counts}, sort_keys=True), flush=True)
    if args.check:
        return 0
    selected = tuple(campaigns) if args.suite == "all" else (args.suite,)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(ROOT), str(ROOT / "research"), env.get("PYTHONPATH", "")
    )))
    for name in selected:
        commands = []
        if name == "science":
            commands.append([sys.executable, "-m", "unittest", "discover", "-s",
                             "tests/Science", "-p", "test_science_qualification.py", "-v"])
        elif name == "ablation":
            commands.append([sys.executable, "-m", "unittest", "-v",
                             "research.tests.test_ablation",
                             "research.tests.test_ablation_exact_rational"])
            commands.append([sys.executable, "-m", "unittest", "discover", "-s",
                             "tests/Science", "-p", "test_ablation_component_qualification.py", "-v"])
        elif name == "economics":
            commands.append([sys.executable, "-m", "unittest", "discover", "-s",
                             "tests/Science", "-p", "test_strategy_economics*.py", "-v"])
        else:
            commands.append([sys.executable, "-m", "unittest", "-v", *campaigns[name]])
        for command in commands:
            if subprocess.run(command, cwd=ROOT, env=env, check=False).returncode:
                print(f"PLAN7_ENGINEERING_CAMPAIGN_FAIL: {name}", file=sys.stderr)
                return 1
        print(f"PLAN7_ENGINEERING_CAMPAIGN_PASS: {name}", flush=True)
    print("PLAN7_ENGINEERING_TESTS_PASS_ONLY: no scientific edge, provider/PAPER/LIVE or release qualification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
