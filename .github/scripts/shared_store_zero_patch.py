from pathlib import Path


def patch_persistence() -> None:
    path = Path("mvp/autotrade_mvp/_persistence_impl.py")
    source = path.read_text(encoding="utf-8")
    assert "def outbox_delivery_state(" not in source
    marker = "    def mark_outbox_delivered(\n"
    assert source.count(marker) == 1
    method = '''    def outbox_delivery_state(
        self,
        event_id: str,
        *,
        topic: str,
    ) -> dict[str, Any] | None:
        """Read one exact outbox row after canonical event-envelope checks."""

        event_id = self._require_text(event_id, "event_id")
        topic = self._require_text(topic, "topic")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    outbox.outbox_id,
                    outbox.event_id,
                    outbox.topic,
                    outbox.payload_json AS outbox_payload_json,
                    outbox.created_at,
                    outbox.envelope_hash,
                    outbox.delivered_at,
                    events.event_type,
                    events.aggregate_type,
                    events.aggregate_id,
                    events.aggregate_version,
                    events.payload_json AS event_payload_json,
                    events.payload_hash,
                    events.committed_at,
                    events.envelope_json AS event_envelope_json,
                    events.envelope_hash AS event_envelope_hash
                FROM outbox
                JOIN events ON events.event_id = outbox.event_id
                WHERE outbox.event_id = ? AND outbox.topic = ?
                """,
                (event_id, topic),
            ).fetchone()
        if row is None:
            return None

        raw_outbox_payload = str(row["outbox_payload_json"])
        actual_outbox_hash = _outbox_envelope_digest(
            str(row["topic"]), raw_outbox_payload
        )
        if row["envelope_hash"] != actual_outbox_hash:
            raise ValueError("outbox envelope hash does not match stored payload")
        event_row = {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "aggregate_version": row["aggregate_version"],
            "payload_json": row["event_payload_json"],
            "payload_hash": row["payload_hash"],
            "committed_at": row["committed_at"],
            "envelope_json": row["event_envelope_json"],
            "envelope_hash": row["event_envelope_hash"],
        }
        event = self._decode_event_row(event_row)
        try:
            outbox_payload = json.loads(raw_outbox_payload)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("outbox payload is not valid JSON") from error
        if canonical_json(outbox_payload) != raw_outbox_payload:
            raise ValueError("outbox payload is not canonical JSON")
        raw_authoritative_envelope = row["event_envelope_json"]
        if not isinstance(raw_authoritative_envelope, str):
            raise ValueError("journal event envelope authority is missing")
        try:
            authoritative_envelope = json.loads(raw_authoritative_envelope)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("journal event envelope is not valid JSON") from error
        if canonical_json(authoritative_envelope) != raw_authoritative_envelope:
            raise ValueError("journal event envelope is not canonical JSON")
        core_envelope = {
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "aggregate_type": event["aggregate_type"],
            "aggregate_id": event["aggregate_id"],
            "aggregate_version": str(event["aggregate_version"]),
            "payload": event["payload"],
            "payload_hash": event["payload_hash"],
            "committed_at": event["committed_at"],
        }
        for key, expected in core_envelope.items():
            if authoritative_envelope.get(key) != expected:
                raise ValueError(
                    "journal event envelope conflicts with core journal event"
                )
        if raw_outbox_payload != raw_authoritative_envelope:
            raise ValueError(
                "outbox payload does not match authoritative journal event envelope"
            )
        return {
            "outbox_id": row["outbox_id"],
            "event_id": row["event_id"],
            "topic": row["topic"],
            "payload": outbox_payload,
            "created_at": row["created_at"],
            "envelope_hash": row["envelope_hash"],
            "delivered": row["delivered_at"] is not None,
        }

'''
    path.write_text(source.replace(marker, method + marker, 1), encoding="utf-8")


def patch_checkpoint() -> None:
    path = Path("mvp/autotrade_mvp/simulation_runtime_checkpoint.py")
    source = path.read_text(encoding="utf-8")
    assert "_autonomous_owned_pending_publications(" not in source
    helper = '''

def _autonomous_publication_owned(
    item: Mapping[str, object],
    *,
    run_id: str,
) -> bool:
    envelope = item.get("payload")
    if type(envelope) is not dict:
        raise AutonomousRuntimeCheckpointError(
            "pending outbox payload is not a canonical event envelope"
        )
    aggregate_type = envelope.get("aggregate_type")
    aggregate_id = envelope.get("aggregate_id")
    if aggregate_type == "canonical_autonomous_simulation":
        return aggregate_id == run_id
    event_payload = envelope.get("payload")
    event_payload = event_payload if type(event_payload) is dict else {}
    environment = envelope.get("environment", event_payload.get("environment"))
    host_id = envelope.get("host_id", event_payload.get("host_id"))
    return (
        aggregate_type in _COMPONENT_AGGREGATE_TYPES
        and environment == "SIMULATION"
        and host_id == "local-simulation"
    )


def _autonomous_owned_pending_publications(
    store: JournalStore,
    *,
    run_id: str,
) -> tuple[dict[str, object], ...]:
    pending = JournalStore.pending_outbox(store, limit=1000)
    if (
        len(pending) == 1000
        and JournalStore.pending_outbox_count(store) > len(pending)
    ):
        raise AutonomousRuntimeCheckpointError(
            "runtime checkpoint publication ownership scan exceeded bounded outbox window"
        )
    return tuple(
        item
        for item in pending
        if _autonomous_publication_owned(item, run_id=run_id)
    )


def deliver_autonomous_owned_publications(
    store: JournalStore,
    *,
    run_id: str,
) -> None:
    """Acknowledge only publications owned by the closed ZERO authority scope."""
    for item in _autonomous_owned_pending_publications(store, run_id=run_id):
        JournalStore.mark_outbox_delivered(
            store,
            item["outbox_id"],
            expected_envelope_hash=item["envelope_hash"],
        )
'''
    marker = "\n\ndef _stable_runtime_components(\n"
    assert source.count(marker) == 1
    source = source.replace(marker, helper + marker, 1)

    old = '''    before = JournalStore.whole_store_state_cut(store)\n\n    pending = JournalStore.pending_outbox_count(store)\n    if pending and not (\n        completion_preimage is not None\n        and pending == 1\n        and [item["event_id"] for item in JournalStore.pending_outbox(store, limit=2)]\n        == [completion_preimage[0]]\n    ):\n        raise AutonomousRuntimeCheckpointError(\n            "runtime checkpoint requires a fully delivered outbox cut"\n        )\n'''
    new = '''    before = JournalStore.whole_store_state_cut(store)\n\n    owned_pending = _autonomous_owned_pending_publications(store, run_id=run_id)\n    if owned_pending:\n        if completion_preimage is None:\n            raise AutonomousRuntimeCheckpointError(\n                "runtime checkpoint requires all ZERO-owned publications delivered"\n            )\n        terminal_id, _receipt_prior_cut = completion_preimage\n        if (\n            len(owned_pending) != 1\n            or owned_pending[0].get("event_id") != terminal_id\n        ):\n            raise AutonomousRuntimeCheckpointError(\n                "runtime checkpoint completion preimage has a foreign ZERO publication"\n            )\n'''
    assert source.count(old) == 1
    source = source.replace(old, new, 1)

    old = '''    captured_cut = before\n    if completion_preimage is not None:\n        terminal_id, captured_cut = completion_preimage\n        if not loop_events or loop_events[-1]["event_id"] != terminal_id:\n            raise AutonomousRuntimeCheckpointError("completion checkpoint is not the terminal loop event")\n        loop_events = loop_events[:-1]\n'''
    new = '''    if completion_preimage is not None:\n        terminal_id, _receipt_prior_cut = completion_preimage\n        if not loop_events or loop_events[-1]["event_id"] != terminal_id:\n            raise AutonomousRuntimeCheckpointError("completion checkpoint is not the terminal loop event")\n        loop_events = loop_events[:-1]\n'''
    assert source.count(old) == 1
    source = source.replace(old, new, 1)

    old = '''    common = {\n        "cut": captured_cut,\n        "journal_store_identity": store_identity,\n'''
    new = '''    scoped_cut = {\n        "loop_events": _digest(loop_events),\n        "authority_events": {\n            aggregate_type: _digest(events)\n            for aggregate_type, events in sorted(authority_events.items())\n        },\n        "owned_pending_publications": [\n            {\n                "event_id": item["event_id"],\n                "topic": item["topic"],\n                "envelope_hash": item["envelope_hash"],\n            }\n            for item in owned_pending\n            if completion_preimage is None\n        ],\n    }\n    common = {\n        "cut": scoped_cut,\n        "journal_store_identity": store_identity,\n'''
    assert source.count(old) == 1
    source = source.replace(old, new, 1)

    old = '''            "kind": "autonomous-runtime-common-cut-v1",\n            "cut": captured_cut,\n            "components": components,\n'''
    new = '''            "kind": "autonomous-runtime-owned-cut-v2",\n            "cut": scoped_cut,\n            "components": components,\n'''
    assert source.count(old) == 1
    source = source.replace(old, new, 1)

    old = '''    prior_cut = receipt["prior_cut"]\n    expected = {"journal_sequence": prior_cut["journal_sequence"] + 1,\n                "counts": dict(prior_cut["counts"])}\n    for name in ("events", "outbox", "command_dedupe"):\n        expected["counts"][name] += 1\n    if (terminal["journal_sequence"] != expected["journal_sequence"]\n            or JournalStore.whole_store_state_cut(store) != expected):\n        raise AutonomousRuntimeCheckpointError("completion checkpoint does not match current authorities")\n'''
    new = '''    prior_cut = receipt["prior_cut"]\n    expected_terminal_sequence = prior_cut["journal_sequence"] + 1\n    if terminal["journal_sequence"] != expected_terminal_sequence:\n        raise AutonomousRuntimeCheckpointError(\n            "completion checkpoint terminal event is not the authorized next journal event"\n        )\n    terminal_publication = JournalStore.outbox_delivery_state(\n        store,\n        terminal["event_id"],\n        topic="autotrade.simulation.events",\n    )\n    if terminal_publication is None:\n        raise AutonomousRuntimeCheckpointError(\n            "completion checkpoint terminal publication is missing"\n        )\n'''
    assert source.count(old) == 1
    source = source.replace(old, new, 1)

    old = '''    for item in JournalStore.pending_outbox(store, limit=2):\n        if item["event_id"] != terminal["event_id"]:\n            raise AutonomousRuntimeCheckpointError("completion checkpoint has foreign pending publication")\n        JournalStore.mark_outbox_delivered(\n            store, item["outbox_id"], expected_envelope_hash=item["envelope_hash"],\n            expected_journal_sequence=expected["journal_sequence"],\n            expected_whole_store_counts=expected["counts"],\n        )\n'''
    new = '''    if not terminal_publication["delivered"]:\n        JournalStore.mark_outbox_delivered(\n            store,\n            terminal_publication["outbox_id"],\n            expected_envelope_hash=terminal_publication["envelope_hash"],\n        )\n        terminal_publication = JournalStore.outbox_delivery_state(\n            store,\n            terminal["event_id"],\n            topic="autotrade.simulation.events",\n        )\n    if terminal_publication is None or not terminal_publication["delivered"]:\n        raise AutonomousRuntimeCheckpointError(\n            "completion checkpoint terminal publication remains undelivered"\n        )\n'''
    assert source.count(old) == 1
    source = source.replace(old, new, 1)
    path.write_text(source, encoding="utf-8")


def patch_session() -> None:
    path = Path("mvp/autotrade_mvp/simulation_session.py")
    source = path.read_text(encoding="utf-8")
    assert "_deliver_autonomous_owned_publications(" not in source
    marker = "\n\ndef _loop_event(\n"
    assert source.count(marker) == 1
    wrapper = '''

def _deliver_autonomous_owned_publications(
    store: JournalStore,
    *,
    run_id: str,
) -> None:
    from .simulation_runtime_checkpoint import deliver_autonomous_owned_publications

    deliver_autonomous_owned_publications(store, run_id=run_id)
'''
    source = source.replace(marker, wrapper + marker, 1)
    lines = source.splitlines(keepends=True)
    rewritten: list[str] = []
    index = 0
    drains = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "for item in store.pending_outbox(limit=1000):":
            indent_width = len(line) - len(line.lstrip())
            indent = line[:indent_width]
            rewritten.append(
                indent + "_deliver_autonomous_owned_publications(store, run_id=run_id)\n"
            )
            index += 1
            skipped = 0
            while index < len(lines):
                body = lines[index]
                if not body.strip():
                    rewritten.append(body)
                    index += 1
                    continue
                body_indent = len(body) - len(body.lstrip())
                if body_indent <= indent_width:
                    break
                index += 1
                skipped += 1
            assert skipped >= 1
            drains += 1
            continue
        rewritten.append(line)
        index += 1
    assert drains == 4, f"expected four global outbox drains, found {drains}"
    source = "".join(rewritten)
    source = source.replace(
        "Deliver existing publications before freezing the completion preimage.",
        "Deliver existing ZERO-owned publications before freezing the completion preimage.",
    )
    source = source.replace(
        "every publication in this terminal cut",
        "every ZERO-owned publication in this terminal cut",
    )
    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    patch_persistence()
    patch_checkpoint()
    patch_session()
