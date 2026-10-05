from pathlib import Path
import subprocess

DONOR = "f3857e717c7c373a7adddf70257ca996b3c61a01"

expected_preimages = {
    "mvp/autotrade_mvp/futures.py": "0e436ba5e2cb15e6e4fc8c82902e5e4e8f0dbe1c",
    "mvp/autotrade_mvp/instruments.py": "e5f076282b572ecdd669bd72e979166e66386051",
    "mvp/autotrade_mvp/futures_journal.py": "cef905b4501890c794094210bf97f422b3ec577c",
    "mvp/tests/test_futures.py": "d841b5ef75d69406ff08328db6eb494186c0d4c7",
    "mvp/tests/test_futures_exact_arithmetic.py": "ab3299bdfa786b47bc2bbdee9d6c7f8c4c382871",
    "mvp/tests/test_futures_journal.py": "fe3ba8c67422294ecdfe778b7b8d5746a4103e25",
    ".github/workflows/futures-qualification.yml": "21222338a5538b05d0d8d8665ee65d8f0c504efb",
}

for path, expected in expected_preimages.items():
    actual = subprocess.check_output(["git", "hash-object", path], text=True).strip()
    if actual != expected:
        raise SystemExit(f"preimage moved for {path}: expected {expected}, got {actual}")

for path in (
    "mvp/autotrade_mvp/settlement_convention.py",
    "mvp/tests/test_futures_settlement_convention_authority.py",
):
    if Path(path).exists():
        raise SystemExit(f"new convention path unexpectedly already exists: {path}")

# These reviewed donor files sit on byte-identical preimages or, for the journal
# pair, on the exact WP-28 stable-cut result already present on this parent.
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
    content = subprocess.check_output(["git", "show", f"{DONOR}:{path}"])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

workflow_path = Path(".github/workflows/futures-qualification.yml")
workflow = workflow_path.read_text(encoding="utf-8")

source_trigger = '      - "mvp/autotrade_mvp/futures.py"\n'
if workflow.count(source_trigger) != 2:
    raise SystemExit("unexpected futures workflow source trigger preimage")
workflow = workflow.replace(
    source_trigger,
    source_trigger
    + '      - "mvp/autotrade_mvp/instruments.py"\n'
    + '      - "mvp/autotrade_mvp/settlement_convention.py"\n',
)

test_trigger = '      - "mvp/tests/test_futures.py"\n'
if workflow.count(test_trigger) != 2:
    raise SystemExit("unexpected futures workflow test trigger preimage")
workflow = workflow.replace(
    test_trigger,
    test_trigger
    + '      - "mvp/tests/test_futures_exact_arithmetic.py"\n'
    + '      - "mvp/tests/test_futures_settlement_convention_authority.py"\n',
)

old_run = (
    "        run: python -m unittest mvp.tests.test_futures "
    "mvp.tests.test_futures_journal "
    "mvp.tests.test_futures_settlement_snapshot_authority -v\n"
)
new_modules = (
    "mvp.tests.test_futures "
    "mvp.tests.test_futures_exact_arithmetic "
    "mvp.tests.test_futures_journal "
    "mvp.tests.test_futures_settlement_snapshot_authority "
    "mvp.tests.test_futures_settlement_convention_authority"
)
if workflow.count(old_run) != 1:
    raise SystemExit("unexpected futures qualification run command preimage")
workflow = workflow.replace(old_run, f"        run: python -m unittest {new_modules} -v\n")

old_evidence = (
    '          --command "python -m unittest mvp.tests.test_futures '
    'mvp.tests.test_futures_journal '
    'mvp.tests.test_futures_settlement_snapshot_authority -v"\n'
)
if workflow.count(old_evidence) != 1:
    raise SystemExit("unexpected futures qualification evidence command preimage")
workflow = workflow.replace(
    old_evidence,
    f'          --command "python -m unittest {new_modules} -v"\n',
)
workflow_path.write_text(workflow, encoding="utf-8", newline="\n")
