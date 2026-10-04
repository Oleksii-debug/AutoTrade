"""Provider-free lifecycle commands through the single durable Host command API.

No credentials, provider traffic, trading permission or arbitrary path/input are
accepted here. The journal-frozen simulation protocol selects every run input.
"""
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5
from .persistence import JournalStore, payload_digest

SIMULATION_ACTIONS = frozenset({'START_SIMULATION', 'RECOVER_SIMULATION', 'BACKUP_SIMULATION'})
_RECEIPT_TYPE = 'simulation_operator_receipt'


def _protocol(journal):
    from .simulation_session import ACCOUNT, ENVIRONMENT
    events = journal.load_events_by_aggregate_type('canonical_autonomous_simulation')
    starts = [e for e in events if e['event_type'] == 'AutonomousSimulationStarted']
    if len(starts) != 1:
        raise ValueError('one frozen provider-free protocol is required')
    protocol = starts[0]['payload']['protocol']
    if protocol['account'] != ACCOUNT or protocol['environment'] != ENVIRONMENT:
        raise ValueError('simulation scope differs')
    return protocol


def canonical_simulation_payload(journal, action, raw, command, account, environment):
    from .simulation_session import ACCOUNT, ENVIRONMENT
    if account != ACCOUNT or environment != ENVIRONMENT or type(raw) is not dict:
        raise ValueError('simulation commands require the canonical SIMULATION scope')
    if set(raw) - {'stop_after_episodes'} or (action == 'BACKUP_SIMULATION' and raw):
        raise ValueError('unsupported simulation payload')
    protocol = _protocol(journal)
    stop = raw.get('stop_after_episodes')
    if stop is not None and (type(stop) is not int or not 1 <= stop <= len(protocol['prices'])):
        raise ValueError('stop must be a frozen observation index')
    UUID(command)
    return {'schema_version': 1, 'command_id': command, 'account_id': account,
            'environment': environment, 'protocol_digest': payload_digest(protocol),
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
    events = journal.load_events(_RECEIPT_TYPE, payload['command_id'])
    if not events:
        return None
    if len(events) != 1:
        raise ValueError('simulation receipt history differs')
    event = events[0]
    expected = {'action': action, 'command_payload_hash': payload_digest(payload)}
    if any(event['payload'].get(k) != v for k, v in expected.items()):
        raise ValueError('simulation receipt conflicts with command')
    return AuthorityExecutionResult((event['event_id'],), ({'kind': 'simulation-lifecycle',
        'event_id': event['event_id'], 'action': action, 'result': event['payload']['result']},))


def execute_simulation_action(journal, action, payload, accepted_at):
    from .simulation_session import run_autonomous_simulation
    from .backup import create_backup, verify_backup
    existing = resolve_simulation_action(journal, action, payload)
    if existing is not None:
        return existing
    protocol = _protocol(journal)
    if payload_digest(protocol) != payload['protocol_digest']:
        raise ValueError('frozen simulation identity changed')
    root = Path(journal.path).parent
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
        import json
        command = [sys.executable, '-m', 'mvp.autotrade_mvp.product_worker', '--state-dir', str(root), '--parent-pid', str(os.getpid())]
        if payload['stop_after_episodes'] is not None:
            command += ['--stop', str(payload['stop_after_episodes'])]
        completed = subprocess.run(command, capture_output=True, timeout=300)
        if completed.returncode != 0 or len(completed.stdout) > 65536:
            raise ValueError('simulation worker stopped; recovery required')
        result = json.loads(completed.stdout)
        if result['status'] == 'UNKNOWN':
            raise ValueError('unfinished simulation requires retained evidence; resend forbidden')
        result = {k: result[k] for k in ('status', 'completed_episodes', 'cash', 'position',
                                       'protocol_digest', 'economic_edge_status')}
    data = {'action': action, 'command_payload_hash': payload_digest(payload), 'result': result}
    event_id = str(uuid5(NAMESPACE_URL, 'autotrade:simulation-receipt:' + payload['command_id']))
    event = {'event_id': event_id, 'event_type': 'SimulationOperatorCompleted', 'schema_version': '1.0.0',
        'aggregate_type': _RECEIPT_TYPE, 'aggregate_id': payload['command_id'], 'aggregate_version': '1',
        'host_id': 'local-simulation', 'owner_epoch': '1', 'environment': 'SIMULATION',
        'occurred_at': accepted_at, 'observed_at': accepted_at, 'committed_at': accepted_at,
        'correlation_id': payload['command_id'], 'causation_id': None, 'payload': data,
        'payload_hash': payload_digest(data), 'evidence_refs': []}
    journal.append_event(event)
    return resolve_simulation_action(journal, action, payload)
