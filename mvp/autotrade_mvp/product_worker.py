"""Installed provider-free worker; local UI sockets stay outside the ZERO fence."""
import argparse
import json
import os
from threading import Thread
from pathlib import Path
from .persistence import JournalStore
from .simulation_commands import _protocol
from .simulation_session import run_autonomous_simulation


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

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--stop', type=int)
    parser.add_argument('--parent-pid', type=int)
    args = parser.parse_args()
    if args.parent_pid is not None:
        start_parent_watchdog(args.parent_pid)
    root = Path(args.state_dir)
    protocol = _protocol(JournalStore(root / 'journal.sqlite3'))
    store = JournalStore(root / 'journal.sqlite3')
    def pause_requested():
        # Accepted emergency stop is already durable. Finish a retained financial
        # observation, then pause before the next market observation/admission.
        return any(e['event_type'] == 'COMMAND_ACCEPTED' and e['payload'].get('action') in
            {'BLOCK_NEW_EXPOSURE', 'REVOKE_AUTHORITY'}
            for e in store.load_events_by_aggregate_type('HOST_CONTROL'))
    result = run_autonomous_simulation(protocol['prices'], root, run_id=protocol['run_id'],
        now=protocol['start_time'], stop_after_episodes=args.stop,
        fault_at_episode=protocol['fault_at_episode'], emergency_at_episode=protocol['emergency_at_episode'],
        partial_fills=protocol.get('execution_profile') == 'PARTIAL_THEN_FULL_V1', should_pause=pause_requested)
    print(json.dumps({k: v for k, v in result.items() if k != 'decisions'}))
    return 2 if result['status'] == 'UNKNOWN' else 0


if __name__ == '__main__':
    raise SystemExit(main())
