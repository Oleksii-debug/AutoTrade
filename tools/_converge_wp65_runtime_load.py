from pathlib import Path
import subprocess

MAIN = "60e7c95b3b572810dcfb6c4ab34e0b338b0ace02"
DONOR = "9971e4377a665aa6e126245c5ef440b175f85331"
PATHS = (
    "mvp/autotrade_mvp/journal_taxonomy.py",
    "mvp/autotrade_mvp/performance_qualification.py",
    "mvp/autotrade_mvp/runtime_load_campaign.py",
    "mvp/autotrade_mvp/runtime_load_evidence.py",
    "mvp/autotrade_mvp/runtime_load_measurement.py",
    "mvp/autotrade_mvp/runtime_load_plan.py",
    "mvp/autotrade_mvp/runtime_load_research_measurement.py",
    "mvp/tests/test_performance_qualification.py",
    "mvp/tests/test_runtime_load_campaign.py",
    "mvp/tests/test_runtime_load_campaign_backlog.py",
    "mvp/tests/test_runtime_load_evidence.py",
    "mvp/tests/test_runtime_load_evidence_full_cut.py",
    "mvp/tests/test_runtime_load_evidence_input_snapshot.py",
    "mvp/tests/test_runtime_load_measurement.py",
    "mvp/tests/test_runtime_load_measurement_backlog.py",
    "mvp/tests/test_runtime_load_measurement_cut.py",
    "mvp/tests/test_runtime_load_measurement_journal_transitive_authority.py",
    "mvp/tests/test_runtime_load_measurement_plan_instance_authority.py",
    "mvp/tests/test_runtime_load_measurement_schema_authority.py",
    "mvp/tests/test_runtime_load_plan.py",
    "mvp/tests/test_runtime_load_plan_limits.py",
    "mvp/tests/test_runtime_load_research_measurement.py",
    "mvp/tests/test_runtime_load_research_measurement_frame_authority.py",
    "mvp/tests/test_runtime_resource_budget.py",
)


def ref_blob(ref: str, path: str) -> str | None:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}:{path}"],
        text=True,
        capture_output=True,
    )
    if probe.returncode != 0:
        return None
    return probe.stdout.strip()


for path in PATHS:
    expected = ref_blob(MAIN, path)
    target = Path(path)
    if expected is None:
        if target.exists():
            raise SystemExit(f"WP-65 path was absent on accepted main but exists on product parent: {path}")
    else:
        if not target.is_file():
            raise SystemExit(f"WP-65 main preimage disappeared on product parent: {path}")
        actual = subprocess.check_output(["git", "hash-object", path], text=True).strip()
        if actual != expected:
            raise SystemExit(
                f"WP-65 overlap detected for {path}: accepted-main {expected}, product-parent {actual}"
            )

for path in PATHS:
    content = subprocess.check_output(["git", "show", f"{DONOR}:{path}"])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
