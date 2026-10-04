"""Provider-free lifecycle commands through the single durable Host command API.

No credentials, provider traffic, trading permission or arbitrary path/input are
accepted here. The journal-frozen simulation protocol selects every run input.
"""
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5
from .persistence import JournalStore, payload_digest
from .simulation_runtime_checkpoint import autonomous_protocol_digest

SIMULATION_ACTIONS = frozenset({'START_SIMULATION', 'RECOVER_SIMULATION', 'BACKUP_SIMULATION'})
_RECEIPT_TYPE = 'simulation_operator_receipt'


def _require_explicit_recovery_for_unknown_start(journal, action):
    """Keep ordinary START from acquiring recovery authority implicitly."""
    if action != 'START_SIMULATION':
        return
    from .simulation_status import inspect_canonical_simulation
    inspected = inspect_canonical_simulation(Path(journal.path).parent)
    if type(inspected) is not dict:
        raise ValueError('canonical simulation state is unavailable')
    status = inspected.get('status')
    if type(status) is not dict:
        raise ValueError('canonical simulation status is unavailable')
    session_status = status.get('session_status')
    if session_status not in {'PAUSED', 'COMPLETED', 'UNKNOWN'}:
        raise ValueError('canonical simulation session status is invalid')
    if session_status == 'UNKNOWN':
        if status.get('recovery_disposition') not in {
            'RETAINED_FILL_RECOVERY',
            'ZERO_WIRE_COMPLETION',
            'RECONCILIATION_REQUIRED',
        }:
            raise ValueError('canonical simulation recovery disposition is invalid')
        raise ValueError('simulation recovery requires RECOVER_SIMULATION')
    if status.get('replay_verified') is not True:
        raise ValueError('simulation start requires verified durable state')


def _protocol(journal):
    from .simulation_session import ACCOUNT, ENVIRONMENT
    events = journal.load_events_by_aggregate_type('canonical_autonomous_simulation')
    starts = [e for e in events if e['event_type'] == 'AutonomousSimulationStarted']
    if len(starts) != 1:
        raise ValueError('one frozen provider-free protocol is required')
    protocol = starts[0]['payload']['protocol']
    if protocol['account'] != ACCOUNT or protocol['environment'] != ENVIRONMENT:
        raise ValueError('simulation scope differs')
    if starts[0]['payload'].get('protocol_digest') != autonomous_protocol_digest(protocol):
        raise ValueError('frozen simulation protocol digest differs')
    return protocol


def canonical_simulation_payload(journal, action, raw, command, account, environment):
    from .simulation_session import ACCOUNT, ENVIRONMENT
    if account != ACCOUNT or environment != ENVIRONMENT or type(raw) is not dict:
        raise ValueError('simulation commands require the canonical SIMULATION scope')
    if set(raw) - {'stop_after_episodes'} or (action == 'BACKUP_SIMULATION' and raw):
        raise ValueError('unsupported simulation payload')
    protocol = _protocol(journal)
    _require_explicit_recovery_for_unknown_start(journal, action)
    stop = raw.get('stop_after_episodes')
    if stop is not None and (type(stop) is not int or not 1 <= stop <= len(protocol['prices'])):
        raise ValueError('stop must be a frozen observation index')
    UUID(command)
    return {'schema_version': 1, 'command_id': command, 'account_id': account,
            'environment': environment, 'protocol_digest': autonomous_protocol_digest(protocol),
            'stop_after_episodes': stop}


def validate_simulation_payload(action, payload, digest, account, environment):
    from .simulation_session import ACCOUNT, ENVIRONMENT
    if (action not in SIMULATION_ACTIONS or type(payload) is not dict
        or set(payload) != {'schema_version', 'command_id', 'account_id', 'environment',
                            'protocol_digest', 'stop_after_episodes'}
        or type(payload['schema_version']) is not int or payload['schema_version'] != 1
        or payload['account_id'] != account or payload['environment'] != environment
        or account != ACCOUNT or environment != ENVIRONMENT or payload_digest(payload) != digest):
        raise ValueError('simulation payload identity/scope differs')
    if str(UUID(payload['command_id'])) != payload['command_id']:
        raise ValueError('noncanonical command identity')
    if payload['stop_after_episodes'] is not None and (
            type(payload['stop_after_episodes']) is not int or not 1 <= payload['stop_after_episodes'] <= 10000):
        raise ValueError('invalid simulation stop')
    return dict(payload)


def resolve_simulation_action(journal, action, payload):
    from .operator_authority_commands import AuthorityExecutionResult
    from .exact_decimal import canonical_decimal_text, parse_bounded_exact_decimal
    events = journal.load_events(_RECEIPT_TYPE, payload['command_id'])
    if not events:
        return None
    if len(events) != 1:
        raise ValueError('simulation receipt history differs')
    event = events[0]
    expected_event_id = str(
        uuid5(
            NAMESPACE_URL,
            'autotrade:simulation-receipt:' + payload['command_id'],
        )
    )
    if (
        event.get('event_id') != expected_event_id
        or event.get('event_type') != 'SimulationOperatorCompleted'
        or event.get('aggregate_type') != _RECEIPT_TYPE
        or event.get('aggregate_id') != payload['command_id']
        or type(event.get('aggregate_version')) is not int
        or event.get('aggregate_version') != 1
        or event.get('host_id') != 'local-simulation'
        or event.get('owner_epoch') != '1'
        or event.get('environment') != 'SIMULATION'
        or event.get('correlation_id') != payload['command_id']
        or event.get('causation_id') is not None
        or event.get('payload_hash') != payload_digest(event.get('payload'))
        or event.get('evidence_refs') != []
        or event.get('occurred_at') != event.get('observed_at')
        or event.get('observed_at') != event.get('committed_at')
    ):
        raise ValueError('simulation receipt envelope identity differs')
    body = event.get('payload')
    if type(body) is not dict or set(body) != {
        'action',
        'command_payload_hash',
        'result',
    }:
        raise ValueError('simulation receipt payload schema differs')
    expected = {'action': action, 'command_payload_hash': payload_digest(payload)}
    if any(body.get(k) != v for k, v in expected.items()):
        raise ValueError('simulation receipt conflicts with command')
    result = body.get('result')
    if type(result) is not dict:
        raise ValueError('simulation receipt result is malformed')

    if action == 'BACKUP_SIMULATION':
        if (
            set(result) != {
                'backup_id',
                'manifest_digest',
                'status',
                'restore_trading_gate',
            }
            or result.get('backup_id') != payload['command_id']
            or result.get('status') != 'VERIFIED'
            or result.get('restore_trading_gate') != 'RECONCILIATION_REQUIRED'
        ):
            raise ValueError('simulation backup receipt result differs')
        digest = result.get('manifest_digest')
        if (
            type(digest) is not str
            or len(digest) != 71
            or not digest.startswith('sha256:')
            or any(character not in '0123456789abcdef' for character in digest[7:])
        ):
            raise ValueError('simulation backup receipt manifest digest is invalid')
    else:
        protocol = _protocol(journal)
        total = len(protocol['prices'])
        target = (
            total
            if payload['stop_after_episodes'] is None
            else payload['stop_after_episodes']
        )
        required = {
            'status',
            'completed_episodes',
            'cash',
            'position',
            'protocol_digest',
            'economic_edge_status',
        }
        completed = result.get('completed_episodes')
        if (
            set(result) != required
            or type(completed) is not int
            or completed < target
            or completed > total
            or result.get('status')
            != ('COMPLETED' if completed == total else 'PAUSED')
            or result.get('protocol_digest') != payload['protocol_digest']
            or result.get('economic_edge_status') != 'INCONCLUSIVE'
        ):
            raise ValueError('simulation lifecycle receipt result differs')
        for field in ('cash', 'position'):
            value = result.get(field)
            parsed = parse_bounded_exact_decimal(value)
            if canonical_decimal_text(parsed) != value:
                raise ValueError('simulation lifecycle receipt money is not canonical')

    return AuthorityExecutionResult((event['event_id'],), ({'kind': 'simulation-lifecycle',
        'event_id': event['event_id'], 'action': action, 'result': result},))


def execute_simulation_action(journal, action, payload, accepted_at):
    from .simulation_session import run_autonomous_simulation
    from .backup import create_backup, verify_backup
    existing = resolve_simulation_action(journal, action, payload)
    if existing is not None:
        return existing
    protocol = _protocol(journal)
    _require_explicit_recovery_for_unknown_start(journal, action)
    if autonomous_protocol_digest(protocol) != payload['protocol_digest']:
        raise ValueError('frozen simulation identity changed')
    root = Path(journal.path).parent
    verified_journal_cut = None
    if action == 'BACKUP_SIMULATION':
        destination = root.parent / 'backups' / payload['command_id']
        # A crash after atomic backup publication must re-verify those same bytes.
        if not destination.exists():
            create_backup(root, root / 'artifacts', destination)
        manifest = verify_backup(destination)
        result = {'backup_id': payload['command_id'], 'manifest_digest': payload_digest(manifest),
                  'status': 'VERIFIED', 'restore_trading_gate': 'RECONCILIATION_REQUIRED'}
    else:
        import subprocess
        import os
        import sys
        from .simulation_status import inspect_canonical_simulation
        command = [sys.executable, '-B', '-m', 'mvp.autotrade_mvp.product_worker', '--state-dir', str(root), '--action', action, '--command-id', payload['command_id'], '--parent-pid', str(os.getpid())]
        if payload['stop_after_episodes'] is not None:
            command += ['--stop', str(payload['stop_after_episodes'])]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as error:
            # Process-launch failure and timeout are recoverable execution
            # uncertainty, not permission to strand a durable Host operation in
            # RUNNING or make startup recovery crash. subprocess.run kills and
            # waits for a timed-out child before raising TimeoutExpired.
            raise ValueError('simulation worker stopped; recovery required') from error
        if completed.returncode != 0 or len(completed.stdout) > 65536:
            raise ValueError('simulation worker stopped; recovery required')

        # Child stdout is not financial authority. Re-read the canonical journal
        # after the worker exits and derive the operator receipt only from the
        # validated durable projection. This prevents a stale/forged success
        # payload from certifying cash, position, protocol identity or edge.
        inspected = inspect_canonical_simulation(root)
        if type(inspected) is not dict:
            raise ValueError('simulation worker produced no canonical durable state')
        status = inspected.get('status')
        report = inspected.get('economic_report')
        if type(status) is not dict or type(report) is not dict:
            raise ValueError('simulation worker durable state is incomplete')
        target_episodes = (
            len(protocol['prices'])
            if payload['stop_after_episodes'] is None
            else payload['stop_after_episodes']
        )
        completed_episodes = status.get('completed_episodes')
        verified_journal_cut = status.get('journal_sequence')
        if (
            type(completed_episodes) is not int
            or completed_episodes < target_episodes
            or completed_episodes > len(protocol['prices'])
            or type(verified_journal_cut) is not int
            or verified_journal_cut < 1
            or status.get('session_status') == 'UNKNOWN'
            or status.get('replay_verified') is not True
            or report.get('reconciled') is not True
            or report.get('economic_edge_status') != 'INCONCLUSIVE'
        ):
            raise ValueError('simulation worker durable completion is not verified')
        result = {
            'status': 'COMPLETED' if completed_episodes == len(protocol['prices']) else 'PAUSED',
            'completed_episodes': completed_episodes,
            'cash': status['cash'],
            'position': status['position'],
            'protocol_digest': payload['protocol_digest'],
            'economic_edge_status': 'INCONCLUSIVE',
        }
    data = {'action': action, 'command_payload_hash': payload_digest(payload), 'result': result}
    event_id = str(uuid5(NAMESPACE_URL, 'autotrade:simulation-receipt:' + payload['command_id']))
    event = {'event_id': event_id, 'event_type': 'SimulationOperatorCompleted', 'schema_version': '1.0.0',
        'aggregate_type': _RECEIPT_TYPE, 'aggregate_id': payload['command_id'], 'aggregate_version': '1',
        'host_id': 'local-simulation', 'owner_epoch': '1', 'environment': 'SIMULATION',
        'occurred_at': accepted_at, 'observed_at': accepted_at, 'committed_at': accepted_at,
        'correlation_id': payload['command_id'], 'causation_id': None, 'payload': data,
        'payload_hash': payload_digest(data), 'evidence_refs': []}
    journal.append_event(
        event,
        expected_journal_sequence=verified_journal_cut,
    )
    return resolve_simulation_action(journal, action, payload)
