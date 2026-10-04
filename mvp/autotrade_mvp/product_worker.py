"""Installed provider-free worker; local UI sockets stay outside the ZERO fence."""
import argparse
import json
import os
from threading import Thread
from pathlib import Path
from .authority import AuthorityService
from .persistence import JournalStore, payload_digest
from .simulation_runtime_checkpoint import autonomous_protocol_digest
from .simulation_commands import (
    _protocol,
    _require_explicit_recovery_for_unknown_start,
    validate_simulation_payload,
)
from .simulation_session import ACCOUNT, ENVIRONMENT, run_autonomous_simulation


def start_parent_watchdog(parent_pid):
    def watch_parent():
        import time
        if os.name == 'nt':
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            handle = kernel.OpenProcess(0x00100000, False, parent_pid)
            if not handle:
                os._exit(73)
            while kernel.WaitForSingleObject(handle, 100) == 258:
                pass
            os._exit(73)
        while os.getppid() == parent_pid:
            time.sleep(0.1)
        os._exit(73)
    Thread(target=watch_parent, daemon=True).start()


_HOST_TERMINAL_PHASES = frozenset({'SUCCEEDED', 'FAILED', 'CANCELLED'})
_HOST_OPERATION_PHASES = frozenset({
    'QUEUED', 'RUNNING', 'WAITING_EXTERNAL', 'UNKNOWN', *_HOST_TERMINAL_PHASES,
})


def _host_emergency_pause_required(store):
    """Project emergency intent without making old restored BLOCK commands sticky.

    A newly accepted BLOCK must stop the worker even before the serialized Host
    executor can apply it. Once that operation is terminal, canonical
    AuthorityService state owns whether the scope is still blocked, so a
    proven AuthorityNewExposureRestored transition can resume future work.
    REVOKE_AUTHORITY remains sticky: unlike the scope block it has no inverse
    transition, and a later self-registered simulation policy must not bypass an
    Owner emergency revoke.
    """
    events = store.load_events_by_aggregate_type('HOST_CONTROL')
    block_phases = {}
    for event in events:
        event_type = event.get('event_type')
        payload = event.get('payload')
        if type(payload) is not dict:
            raise ValueError('Host emergency event payload must be an object')
        if event_type == 'COMMAND_ACCEPTED':
            action = payload.get('action')
            if action == 'REVOKE_AUTHORITY':
                return True
            if action != 'BLOCK_NEW_EXPOSURE':
                continue
            operation_id = payload.get('operation_id')
            if type(operation_id) is not str or not operation_id.strip():
                raise ValueError('accepted Host block requires operation identity')
            if operation_id in block_phases:
                raise ValueError('duplicate Host block operation identity')
            block_phases[operation_id] = 'QUEUED'
        elif event_type == 'OPERATION_UPDATED':
            operation_id = payload.get('operation_id')
            if operation_id not in block_phases:
                continue
            phase = payload.get('phase')
            if phase not in _HOST_OPERATION_PHASES or phase == 'QUEUED':
                raise ValueError('Host block operation phase is invalid')
            block_phases[operation_id] = phase

    if AuthorityService(store).is_new_exposure_blocked(ACCOUNT, ENVIRONMENT):
        return True
    return any(phase not in _HOST_TERMINAL_PHASES for phase in block_phases.values())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--action', required=True, choices=('START_SIMULATION', 'RECOVER_SIMULATION'))
    parser.add_argument('--command-id', required=True)
    parser.add_argument('--stop', type=int)
    parser.add_argument('--parent-pid', type=int)
    args = parser.parse_args()
    if args.parent_pid is not None:
        start_parent_watchdog(args.parent_pid)
    root = Path(args.state_dir)
    store = JournalStore(root / 'journal.sqlite3')
    protocol = _protocol(store)

    # The subprocess is an implementation detail, not a second control plane.
    # It may act only for one exact lifecycle command that the canonical Host
    # durably accepted before process launch.
    accepted = [
        event for event in store.load_events_by_aggregate_type('HOST_CONTROL')
        if event.get('event_type') == 'COMMAND_ACCEPTED'
        and type(event.get('payload')) is dict
        and event['payload'].get('command_id') == args.command_id
    ]
    if len(accepted) != 1:
        raise ValueError('worker requires one durable accepted Host command')
    accepted_payload = accepted[0]['payload']
    if accepted_payload.get('action') != args.action:
        raise ValueError('worker action differs from durable Host command')
    action_payload = validate_simulation_payload(
        args.action,
        accepted_payload.get('action_payload'),
        accepted_payload.get('action_payload_hash'),
        ACCOUNT,
        ENVIRONMENT,
    )
    if (
        action_payload.get('command_id') != args.command_id
        or action_payload.get('protocol_digest') != autonomous_protocol_digest(protocol)
        or action_payload.get('stop_after_episodes') != args.stop
    ):
        raise ValueError('worker invocation differs from durable lifecycle contract')
    _require_explicit_recovery_for_unknown_start(store, args.action)

    def pause_requested():
        # Finish any retained financial observation, then pause before the next
        # market observation/admission when canonical emergency truth requires it.
        return _host_emergency_pause_required(store)
    result = run_autonomous_simulation(protocol['prices'], root, run_id=protocol['run_id'],
        now=protocol['start_time'], stop_after_episodes=args.stop,
        fault_at_episode=protocol['fault_at_episode'], emergency_at_episode=protocol['emergency_at_episode'],
        execution_profile=protocol.get('execution_profile', 'IMMEDIATE'),
        target_quantity=protocol.get('target_quantity', '1'),
        should_pause=pause_requested)
    print(json.dumps({k: v for k, v in result.items() if k != 'decisions'}))
    return 2 if result['status'] == 'UNKNOWN' else 0


if __name__ == '__main__':
    raise SystemExit(main())
