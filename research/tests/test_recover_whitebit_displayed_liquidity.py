from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


TOOL_PATH = Path(__file__).resolve().parents[2] / "tools/recover_whitebit_displayed_liquidity.py"
SPEC = importlib.util.spec_from_file_location("displayed_liquidity_recovery", TOOL_PATH)
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    decision = 1800
    receipt = {"decision_epoch": decision, "state": "INVALID_EVIDENCE", "reason": "INVALID_MISSING_SHARD"}
    path = tmp_path / f"{decision}.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    digest = sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(tool, "LATE_EMPTY_SHARDS", {decision: (digest, "expected_shard_hash")})
    monkeypatch.setattr(tool, "ORIGINAL_OBSERVATION_UTC", "2000")
    runner = SimpleNamespace(
        interval_receipt=lambda *_: {"state": "COMPLETE"},
        receipt_path=lambda _: path,
        utc_timestamp=int,
    )
    shard = {"start_epoch": 900, "end_epoch": 1800, "books": [], "trades": [], "sealed_at_utc": "2001"}
    archive = SimpleNamespace(
        plan={"segment_seconds": 900, "watermark_seconds": 10},
        shard=lambda _: ("SEALED", shard, "expected_shard_hash"),
    )
    plan = {"feature_shard_seconds": 900, "missing_shard_grace_seconds": 180}
    return SimpleNamespace(
        decision=decision, receipt=receipt, path=path, runner=runner, shard=shard, archive=archive, plan=plan,
    )


def test_late_empty_shard_retains_original_dropout_and_delegates_other_decisions(campaign):
    tool.attach_late_shard_recovery(campaign.runner)
    interval = campaign.runner.interval_receipt
    assert interval(campaign.decision, campaign.archive, [], campaign.plan) == campaign.receipt
    assert interval(3600, campaign.archive, [], campaign.plan) == {"state": "COMPLETE"}
    assert json.loads(campaign.path.read_bytes()) == campaign.receipt


@pytest.mark.parametrize("key,value", [
    ("sealed_at_utc", "1999"),
    ("sealed_at_utc", "2000"),
    ("start_epoch", 0),
    ("end_epoch", 900),
    ("books", [{"received_at": "1700"}]),
    ("trades", [{"received_at": "1700"}]),
])
def test_changed_shard_fails_closed(campaign, key, value):
    campaign.shard[key] = value
    tool.attach_late_shard_recovery(campaign.runner)
    with pytest.raises(ValueError, match="did not verify"):
        campaign.runner.interval_receipt(campaign.decision, campaign.archive, [], campaign.plan)


def test_wrong_shard_digest_and_premature_exclusion_fail_closed(campaign):
    tool.attach_late_shard_recovery(campaign.runner)
    interval = campaign.runner.interval_receipt
    campaign.plan["missing_shard_grace_seconds"] = 300
    with pytest.raises(ValueError, match="did not verify"):
        interval(campaign.decision, campaign.archive, [], campaign.plan)
    campaign.plan["missing_shard_grace_seconds"] = 180
    campaign.archive.shard = lambda _: ("SEALED", campaign.shard, "forged_shard_hash")
    with pytest.raises(ValueError, match="did not verify"):
        interval(campaign.decision, campaign.archive, [], campaign.plan)


def test_changed_receipt_fails_before_recovery(campaign):
    campaign.path.write_text(json.dumps({**campaign.receipt, "orders": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        tool.attach_late_shard_recovery(campaign.runner)


def test_publish_runs_read_only_preflight_and_preserves_verifier_errors(campaign, monkeypatch):
    monkeypatch.setattr(tool, "load_registered_runner", lambda _: campaign.runner)
    calls = []

    def observe(*, write):
        calls.append(write)
        raise ValueError("immutable receipt changed: 3600")

    campaign.runner.observe = observe
    with pytest.raises(ValueError, match="immutable receipt changed: 3600"):
        tool.recover(campaign.path.parent, publish=True)
    assert calls == [False]


@pytest.mark.skipif(os.name != "nt", reason="Windows rename has native no-replace semantics")
def test_windows_immutable_publish_reuses_same_bytes_and_refuses_different_bytes(tmp_path):
    path = tmp_path / "receipt.json"
    tool.windows_publish_immutable(path, b"original")
    tool.windows_publish_immutable(path, b"original")
    with pytest.raises(ValueError, match="Immutable file changed"):
        tool.windows_publish_immutable(path, b"modified")
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


def test_existing_immutable_score_is_verified_before_publication(campaign, monkeypatch):
    runner = campaign.runner
    runner.ROOT = campaign.path.parent
    runner.read_registered = lambda: ({"schema": "study"}, {"protocol_sha256": "registered"})
    runner.schedules = lambda _: [campaign.decision]
    runner.read_receipt = lambda path: json.loads(path.read_bytes())
    runner.canonical = lambda row: json.dumps(row, sort_keys=True).encode()
    score_path = runner.ROOT / "score-v3.json"
    score = {"gate": "REJECTED"}
    exact = runner.canonical({
        **score, "schema": "study-score", "protocol_sha256": "registered",
        "receipts_sha256": sha256(runner.canonical(campaign.receipt)).hexdigest(),
    })
    score_path.write_bytes(exact)
    tool.verify_existing_score(runner, {"score": score})
    score_path.write_bytes(exact.replace(b"REJECTED", b"APPROVED"))
    monkeypatch.setattr(tool, "load_registered_runner", lambda _: runner)
    calls = []

    def observe(*, write):
        calls.append(write)
        return {"score": score}

    runner.observe = observe
    with pytest.raises(ValueError, match="Immutable score changed"):
        tool.recover(runner.ROOT, publish=True)
    assert calls == [False]
