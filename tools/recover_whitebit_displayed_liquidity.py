"""Resume the registered November 2026 displayed-liquidity campaign.

Pass --campaign-root with the directory containing the original runner and
receipts. The default verifies and projects catch-up without writing. Pass
--publish to seal the outstanding receipts through the original publisher.
"""

from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType


REGISTERED_FILES = {
    "study_v3_repaired.py": "915756fe4c6b607f5085f6a36052f5e68a0edd63fdf440bfeaea5038685fbe0e",
    "registration-v3.json": "bc1e6bc5e3be6e9dd5be0bf6e18f18faee10dcd5156c51563ee00bbc696f812c",
    "implementation-repair-v3.json": "4191babb06836c708e696c0c519c4da419a206c07bd789ef00f2d07ec3282826",
}

# The 01:42:46 UTC checkpoint sealed both exclusions before the archive caught up.
ORIGINAL_OBSERVATION_UTC = "2026-11-14T01:42:46.000000+00:00"
LATE_EMPTY_SHARDS = {
    1794613500: (
        "0618b6e624f76c9e334820270b3a45e2bd2710d0e29695f3bfa5ac4f1934ac46",
        "b4a38551d48d91367c681e519d85da55069abb8bb97c19b73b0ca3ecc7cbcaea",
    ),
    1794617100: (
        "e97ab1e04daf61b21851e0a659d1c4e34a9993a996f4d8c2a47ec58532a621a8",
        "939296799ebc9e945fb71272713f612b4be7f18e63b2a6bd1e1e7870d8982120",
    ),
}


def read_pinned(path, expected_digest):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Required campaign evidence is missing: {path.name}")
    payload = path.read_bytes()
    if sha256(payload).hexdigest() != expected_digest:
        raise ValueError(f"Campaign evidence digest mismatch: {path.name}")
    return payload


def load_registered_runner(root):
    evidence = {
        filename: read_pinned(root / filename, digest)
        for filename, digest in REGISTERED_FILES.items()
    }
    path = root / "study_v3_repaired.py"
    runner = ModuleType("whitebit_registered_displayed_liquidity")
    runner.__file__ = str(path)
    exec(compile(evidence[path.name], str(path), "exec"), vars(runner))
    return runner


def attach_late_shard_recovery(runner):
    original_interval = runner.interval_receipt
    checked_at = runner.utc_timestamp(ORIGINAL_OBSERVATION_UTC)
    sealed_dropouts = {
        decision: json.loads(read_pinned(runner.receipt_path(decision), receipt_digest))
        for decision, (receipt_digest, _) in LATE_EMPTY_SHARDS.items()
    }

    def interval(decision, archive, continuity_rows, plan):
        if decision not in sealed_dropouts:
            return original_interval(decision, archive, continuity_rows, plan)
        start = decision - plan["feature_shard_seconds"]
        invalidation_at = start + archive.plan["segment_seconds"] + archive.plan[
            "watermark_seconds"
        ] + plan["missing_shard_grace_seconds"]
        state, shard, digest = archive.shard(start)
        if (
            state != "SEALED"
            or digest != LATE_EMPTY_SHARDS[decision][1]
            or shard["start_epoch"] != start
            or shard["end_epoch"] != decision
            or shard["books"]
            or shard["trades"]
            or not invalidation_at <= checked_at < runner.utc_timestamp(shard["sealed_at_utc"])
        ):
            raise ValueError(f"Late empty feature shard did not verify: {decision}")
        return sealed_dropouts[decision]

    runner.interval_receipt = interval


def windows_publish_immutable(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".displayed-liquidity-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A Windows rename within one directory fails when its destination exists.
        os.rename(temporary, path)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_size != len(payload) or path.read_bytes() != payload:
            raise ValueError(f"Immutable file changed: {path.name}")
    finally:
        temporary.unlink(missing_ok=True)


def verify_existing_score(runner, verified):
    score_path = runner.ROOT / "score-v3.json"
    if not score_path.exists() and not score_path.is_symlink():
        return
    if verified["score"] is None:
        raise ValueError("Immutable score exists before the campaign is terminal")
    plan, registration = runner.read_registered()
    receipts = [runner.read_receipt(runner.receipt_path(decision)) for decision in runner.schedules(plan)]
    expected = {
        "schema": plan["schema"] + "-score",
        "protocol_sha256": registration["protocol_sha256"],
        "receipts_sha256": sha256(b"".join(runner.canonical(row) for row in receipts)).hexdigest(),
        **verified["score"],
    }
    payload = runner.canonical(expected)
    if score_path.is_symlink() or score_path.stat().st_size != len(payload) or score_path.read_bytes() != payload:
        raise ValueError("Immutable score changed")


def recover(root, *, publish=False):
    runner = load_registered_runner(Path(root).resolve(strict=True))
    attach_late_shard_recovery(runner)
    verified = runner.observe(write=False)
    verify_existing_score(runner, verified)
    if publish:
        if os.name == "nt":
            original_write = runner.atomic_write

            def write(path, payload, immutable=False):
                if immutable:
                    windows_publish_immutable(path, payload)
                else:
                    original_write(path, payload)

            runner.atomic_write = write
        return runner.observe(write=True)
    return verified


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    status = recover(args.campaign_root, publish=args.publish)
    print(json.dumps(status, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
