from pathlib import Path

TARGET = Path("mvp/autotrade_mvp/reconciliation_journal.py")
WORKFLOW = Path(".github/workflows/patch-reconciliation-direct-negative-authority.yml")
SELF = Path(__file__)

source = TARGET.read_text(encoding="utf-8")
old = '''    event = events[-1]\n    _require_checkpoint_scope(\n        event,\n        provider_id=provider_id,\n        account_id=account_id,\n        environment=environment,\n    )\n    return event\n\n\ndef load_latest_reconciliation_checkpoint_for_scope(\n'''
new = '''    event = events[-1]\n    payload = _require_checkpoint_scope(\n        event,\n        provider_id=provider_id,\n        account_id=account_id,\n        environment=environment,\n    )\n    _require_negative_resolution_authority(payload)\n    return event\n\n\ndef load_latest_reconciliation_checkpoint_for_scope(\n'''
if source.count(old) != 1:
    raise SystemExit("exact direct reconciliation loader preimage not found once")
TARGET.write_text(source.replace(old, new), encoding="utf-8")
SELF.unlink()
WORKFLOW.unlink()
