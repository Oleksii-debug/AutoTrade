from pathlib import Path
import subprocess

MAIN = "60e7c95b3b572810dcfb6c4ab34e0b338b0ace02"
CONVENTION_DONOR = "f3857e717c7c373a7adddf70257ca996b3c61a01"
WP65_DONOR = "9971e4377a665aa6e126245c5ef440b175f85331"


def blob(path: str) -> str:
    return subprocess.check_output(["git", "hash-object", path], text=True).strip()


def ref_blob(ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}:{path}"],
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def copy_from(ref: str, path: str) -> None:
    content = subprocess.check_output(["git", "show", f"{ref}:{path}"])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


# 1) Settlement-convention convergence. These exact preimages are either the
# old reviewed base or the exact stable-cut WP-28 result already in #1745/#1746.
convention_preimages = {
    "mvp/autotrade_mvp/futures.py": "0e436ba5e2cb15e6e4fc8c82902e5e4e8f0dbe1c",
    "mvp/autotrade_mvp/instruments.py": "e5f076282b572ecdd669bd72e979166e66386051",
    "mvp/autotrade_mvp/futures_journal.py": "cef905b4501890c794094210bf97f422b3ec577c",
    "mvp/tests/test_futures.py": "d841b5ef75d69406ff08328db6eb494186c0d4c7",
    "mvp/tests/test_futures_exact_arithmetic.py": "ab3299bdfa786b47bc2bbdee9d6c7f8c4c382871",
    "mvp/tests/test_futures_journal.py": "fe3ba8c67422294ecdfe778b7b8d5746a4103e25",
    ".github/workflows/futures-qualification.yml": "21222338a5538b05d0d8d8665ee65d8f0c504efb",
}
for path, expected in convention_preimages.items():
    if not Path(path).is_file() or blob(path) != expected:
        raise SystemExit(f"settlement-convention preimage moved: {path}")
for path in (
    "mvp/autotrade_mvp/settlement_convention.py",
    "mvp/tests/test_futures_settlement_convention_authority.py",
):
    if Path(path).exists():
        raise SystemExit(f"settlement-convention new path unexpectedly exists: {path}")
for path in (
    "mvp/autotrade_mvp/futures.py",
    "mvp/autotrade_mvp/instruments.py",
    "mvp/autotrade_mvp/futures_journal.py",
    "mvp/tests/test_futures.py",
    "mvp/tests/test_futures_exact_arithmetic.py",
    "mvp/tests/test_futures_journal.py",
    "mvp/autotrade_mvp/settlement_convention.py",
    "mvp/tests/test_futures_settlement_convention_authority.py",
):
    copy_from(CONVENTION_DONOR, path)

workflow_path = Path(".github/workflows/futures-qualification.yml")
workflow = workflow_path.read_text(encoding="utf-8")
source_trigger = '      - "mvp/autotrade_mvp/futures.py"\n'
test_trigger = '      - "mvp/tests/test_futures.py"\n'
if workflow.count(source_trigger) != 2 or workflow.count(test_trigger) != 2:
    raise SystemExit("futures workflow trigger preimage changed")
workflow = workflow.replace(
    source_trigger,
    source_trigger
    + '      - "mvp/autotrade_mvp/instruments.py"\n'
    + '      - "mvp/autotrade_mvp/settlement_convention.py"\n',
)
workflow = workflow.replace(
    test_trigger,
    test_trigger
    + '      - "mvp/tests/test_futures_exact_arithmetic.py"\n'
    + '      - "mvp/tests/test_futures_settlement_convention_authority.py"\n',
)
old_modules = (
    "mvp.tests.test_futures mvp.tests.test_futures_journal "
    "mvp.tests.test_futures_settlement_snapshot_authority"
)
new_modules = (
    "mvp.tests.test_futures mvp.tests.test_futures_exact_arithmetic "
    "mvp.tests.test_futures_journal mvp.tests.test_futures_settlement_snapshot_authority "
    "mvp.tests.test_futures_settlement_convention_authority"
)
if workflow.count(old_modules) != 2:
    raise SystemExit("futures workflow command preimage changed")
workflow = workflow.replace(old_modules, new_modules)
workflow_path.write_text(workflow, encoding="utf-8", newline="\n")

# 2) WP-65 runtime-load evidence. Every path must still be exactly accepted-main
# state (or expected-absent) on the integrated product parent before overlay.
wp65_paths = (
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
for path in wp65_paths:
    expected = ref_blob(MAIN, path)
    target = Path(path)
    if expected is None:
        if target.exists():
            raise SystemExit(f"WP-65 expected-absent path now exists: {path}")
    elif not target.is_file() or blob(path) != expected:
        raise SystemExit(f"WP-65 product-parent overlap detected: {path}")
for path in wp65_paths:
    copy_from(WP65_DONOR, path)

# 3) Binance Spot depth continuity: an event ending exactly at the current cursor
# is fully consumed and must never be APPLYed a second time.
binance_path = Path("mvp/autotrade_mvp/binance_spot.py")
if blob(str(binance_path)) != "d0538203334f2a08f2a6687314e7de450b96b4e6":
    raise SystemExit("Binance Spot integrated preimage moved")
text = binance_path.read_text(encoding="utf-8")
old = '        if admitted.final_update_id < cursor.update_id:\n            return BinanceSpotDepthDecision("DISCARD", None)\n'
new = '        if admitted.final_update_id <= cursor.update_id:\n            return BinanceSpotDepthDecision("DISCARD", None)\n'
if text.count(old) != 1:
    raise SystemExit("Binance depth stale-range boundary was not unique")
binance_path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

regression = Path("mvp/tests/test_binance_depth_equal_cursor.py")
if regression.exists():
    raise SystemExit("Binance depth equality regression unexpectedly already exists")
regression.write_text('''import unittest\n\nfrom mvp.autotrade_mvp.binance_spot import (\n    BinanceSpotDepthContinuityPolicy,\n    BinanceSpotDepthCursor,\n    BinanceSpotDepthRange,\n)\n\n\nclass BinanceSpotDepthEqualCursorRegressionTests(unittest.TestCase):\n    def _range(self, first: int, final: int) -> BinanceSpotDepthRange:\n        return BinanceSpotDepthRange.from_diff_depth_payload({\n            "e": "depthUpdate", "s": "BTCUSDT", "U": first, "u": final,\n        })\n\n    def test_fully_consumed_range_ending_at_current_cursor_is_discarded(self):\n        decision = BinanceSpotDepthContinuityPolicy.advance(\n            local=BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=105),\n            event=self._range(100, 105),\n        )\n        self.assertEqual(decision.disposition, "DISCARD")\n        self.assertIsNone(decision.next_update_id)\n\n    def test_overlap_advancing_past_cursor_remains_applicable(self):\n        decision = BinanceSpotDepthContinuityPolicy.advance(\n            local=BinanceSpotDepthCursor(symbol="BTCUSDT", update_id=105),\n            event=self._range(100, 106),\n        )\n        self.assertEqual(decision.disposition, "APPLY")\n        self.assertEqual(decision.next_update_id, 106)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''', encoding="utf-8", newline="\n")
