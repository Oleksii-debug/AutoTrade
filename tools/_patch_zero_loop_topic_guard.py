from pathlib import Path

TARGET = Path("mvp/autotrade_mvp/simulation_runtime_checkpoint.py")
WORKFLOW = Path(".github/workflows/patch-zero-loop-topic-guard.yml")
SELF = Path(__file__)

source = TARGET.read_text(encoding="utf-8")
old = '''    pending: list[dict[str, object]] = []\n    for event_id in _autonomous_owned_event_ids(store, run_id=run_id):\n        state = JournalStore.outbox_delivery_state(store, event_id)\n        if state is None or state["delivered"]:\n            continue\n        if not _autonomous_publication_owned(state, run_id=run_id):\n            raise AutonomousRuntimeCheckpointError(\n                "exact ZERO outbox state escaped runtime ownership"\n            )\n        pending.append(state)\n    return tuple(pending)\n'''
new = '''    pending: list[dict[str, object]] = []\n    for event_id in _autonomous_owned_event_ids(store, run_id=run_id):\n        state = JournalStore.outbox_delivery_state(store, event_id)\n        if state is None:\n            continue\n        if not _autonomous_publication_owned(state, run_id=run_id):\n            raise AutonomousRuntimeCheckpointError(\n                "exact ZERO outbox state escaped runtime ownership"\n            )\n        envelope = state.get("payload")\n        if (\n            type(envelope) is dict\n            and envelope.get("aggregate_type") == "canonical_autonomous_simulation"\n            and state.get("topic") != "autotrade.simulation.events"\n        ):\n            raise AutonomousRuntimeCheckpointError(\n                "ZERO loop publication routing topic is not canonical"\n            )\n        if state["delivered"]:\n            continue\n        pending.append(state)\n    return tuple(pending)\n'''
if source.count(old) != 1:
    raise SystemExit("exact ZERO outbox ownership preimage not found once")
TARGET.write_text(source.replace(old, new), encoding="utf-8")
SELF.unlink()
WORKFLOW.unlink()
