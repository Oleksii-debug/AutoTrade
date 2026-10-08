"""Whole-product acceptance through the real HTTP interface and a killed worker.

This is provider-free process/UI-API evidence, not Windows/NVDA qualification.
"""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from mvp.autotrade_mvp.product_runtime import (
    _launch_message,
    _owned_desktop_session_sink,
    build_product,
    restore_product_backup,
    source_revision,
)
from mvp.autotrade_mvp.windows_host_session import (
    desktop_session_credential_target,
    persist_desktop_owner_session,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.product_worker import _host_emergency_pause_required
from mvp.autotrade_mvp.simulation_commands import _protocol, resolve_simulation_action
from mvp.autotrade_mvp.simulation_session import ACCOUNT, ENVIRONMENT, PROVIDER, INSTRUMENT
from mvp.autotrade_mvp.simulation_status import SimulationStateChanging
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.backup import verify_backup, restore_requires_reconciliation

ROOT = Path(__file__).resolve().parents[2]


class ProductClient:
    def __init__(self, directory, *, desktop_session_sink=lambda **_kwargs: None, auto_pair=True):
        probe = socket.socket(); probe.bind(('127.0.0.1', 0))
        self.port = probe.getsockname()[1]; probe.close()
        self.origin = f'http://127.0.0.1:{self.port}'
        self.runtime, url = build_product(
            directory,
            port=self.port,
            desktop_session_sink=desktop_session_sink,
        )
        self.worker = Thread(target=self.runtime.serve_forever)
        self.worker.start()
        self.cookie = None
        self.pairing_code = url.split('#pair=')[1]
        if auto_pair:
            response, data, headers = self.request(
                'POST',
                '/api/v1/session',
                {'pairing_code': self.pairing_code},
            )
            assert response == 200, (response, data)
            self.cookie = headers['Set-Cookie'].split(';')[0]

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        headers = {'Origin': self.origin}
        if self.cookie: headers['Cookie'] = self.cookie
        if payload is not None: headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse(); raw = response.read()
            data = json.loads(raw) if 'json' in response.getheader('Content-Type', '') else raw.decode()
            return response.status, data, dict(response.getheaders())
        finally: connection.close()

    def state(self):
        for _ in range(100):
            status, data, _ = self.request('GET', '/api/v1/state')
            if status == 200:
                return data
            if status != 503 or data != {'error': 'SNAPSHOT_BUSY', 'retryable': True}:
                raise AssertionError((status, data))
            time.sleep(.02)
        raise AssertionError((status, data))

    def command(self, action, payload=None):
        state = self.state(); command_id = str(uuid4())
        request = {'command_id': command_id, 'idempotency_key': command_id,
            'expected_state_version': state['state_version'], 'actor': 'local-owner',
            'session': state['permission_summary']['session'], 'account_id': ACCOUNT,
            'environment': ENVIRONMENT, 'action': action, 'payload': payload or {}}
        status, accepted, _ = self.request('POST', '/api/v1/commands', request)
        assert status == 200 and accepted['status'] == 'ACCEPTED', (status, accepted)
        operation = accepted['operation_id']
        for _ in range(300):
            status, result, _ = self.request('GET', '/api/v1/operations/' + operation)
            if status == 200 and result['phase'] in {'SUCCEEDED', 'FAILED', 'UNKNOWN'}:
                return command_id, result
            time.sleep(.02)
        raise AssertionError('operation failed to finish')

    def close(self):
        self.runtime.close(); self.worker.join(timeout=10)
        assert not self.worker.is_alive()


class ProviderFreeProductAcceptance(unittest.TestCase):
    def test_owned_desktop_child_uses_direct_ephemeral_session_handoff(self):
        from mvp.autotrade_mvp import product_runtime

        class Runtime:
            def __init__(self):
                self.closed = 0
            def serve_forever(self):
                return
            def close(self):
                self.closed += 1

        runtime = Runtime()
        with patch.object(product_runtime, 'build_product',
                return_value=(runtime, 'http://127.0.0.1:8765/#pair=test-secret')) as builder, \
             patch.object(product_runtime.signal, 'signal'), \
             patch.object(product_runtime, 'Thread'):
            self.assertEqual(product_runtime.main([
                '--data-dir', 'unused-owned-child-state',
                '--port', '8765',
                '--no-browser',
                '--desktop-child',
            ]), 0)
        self.assertIs(builder.call_args.kwargs['desktop_session_sink'], _owned_desktop_session_sink)
        self.assertGreaterEqual(runtime.closed, 1)

    def test_owned_desktop_session_sink_does_not_write_credential_manager(self):
        with patch('mvp.autotrade_mvp.product_runtime.persist_desktop_owner_session') as persist:
            self.assertIsNone(_owned_desktop_session_sink(
                origin='http://127.0.0.1:8765',
                actor='local-owner',
                token='ephemeral-owned-session',
            ))
        persist.assert_not_called()

    def test_active_simulation_writer_is_retryable_snapshot_contention_only(self):
        with TemporaryDirectory() as directory:
            data = Path(directory) / 'product'
            client = ProductClient(data)
            try:
                with patch(
                    'mvp.autotrade_mvp.product_runtime.inspect_canonical_simulation',
                    side_effect=SimulationStateChanging('simulation writer is active'),
                ):
                    status, body, headers = client.request('GET', '/api/v1/state')
                self.assertEqual(status, 503)
                self.assertEqual(
                    body,
                    {'error': 'SNAPSHOT_BUSY', 'retryable': True},
                )
                self.assertEqual(headers['Retry-After'], '1')

                with patch(
                    'mvp.autotrade_mvp.product_runtime.inspect_canonical_simulation',
                    side_effect=ValueError('corrupt canonical simulation scope'),
                ):
                    status, body, _ = client.request('GET', '/api/v1/state')
                self.assertEqual(status, 400)
                self.assertEqual(body, {'error': 'INVALID_REQUEST'})
            finally:
                client.close()

    def test_state_http_retries_transient_journal_race_and_bounds_persistent_churn(self):
        with TemporaryDirectory() as directory:
            data = Path(directory) / 'product'
            client = ProductClient(data)
            writer = JournalStore(data / 'state' / 'journal.sqlite3')
            original_snapshot = client.runtime.application._snapshot_provider
            try:
                calls = {'count': 0}

                def one_race(durable, principal):
                    calls['count'] += 1
                    value = original_snapshot(durable, principal)
                    if calls['count'] == 1:
                        payload = {'reason': 'product-state-transient-race'}
                        writer.append_event({
                            'event_id': 'product-state-transient-race',
                            'event_type': 'ProductStateRaceInjected',
                            'aggregate_type': 'product_state_race_test',
                            'aggregate_id': 'transient',
                            'aggregate_version': '1',
                            'payload': payload,
                            'payload_hash': payload_digest(payload),
                            'committed_at': '2026-10-04T00:00:00Z',
                        })
                    return value

                with patch.object(client.runtime.application, '_snapshot_provider', one_race):
                    status, state, _ = client.request('GET', '/api/v1/state')
                self.assertEqual(status, 200)
                self.assertEqual(state['reason_codes'], ['SIMULATION_ONLY', 'ECONOMIC_EDGE_UNPROVEN'])
                self.assertEqual(calls['count'], 2)

                churn = {'count': 0}

                def persistent_race(durable, principal):
                    churn['count'] += 1
                    value = original_snapshot(durable, principal)
                    sequence = churn['count']
                    payload = {'reason': 'product-state-persistent-race', 'attempt': sequence}
                    writer.append_event({
                        'event_id': f'product-state-persistent-race-{sequence}',
                        'event_type': 'ProductStateRaceInjected',
                        'aggregate_type': 'product_state_race_test',
                        'aggregate_id': f'persistent-{sequence}',
                        'aggregate_version': '1',
                        'payload': payload,
                        'payload_hash': payload_digest(payload),
                        'committed_at': '2026-10-04T00:00:00Z',
                    })
                    return value

                with patch.object(client.runtime.application, '_snapshot_provider', persistent_race):
                    status, body, headers = client.request('GET', '/api/v1/state')
                self.assertEqual(status, 503)
                self.assertEqual(body, {'error': 'SNAPSHOT_BUSY', 'retryable': True})
                self.assertEqual(headers['Retry-After'], '1')
                self.assertEqual(churn['count'], 4)
            finally:
                client.close()

    def test_state_http_recovers_from_repeated_cross_thread_journal_races(self):
        with TemporaryDirectory() as directory:
            data = Path(directory) / 'product'
            client = ProductClient(data)
            writer = JournalStore(data / 'state' / 'journal.sqlite3')
            original_snapshot = client.runtime.application._snapshot_provider
            calls = {'count': 0}
            requests = 12
            try:
                with ThreadPoolExecutor(max_workers=1) as writers:
                    def first_attempt_race(durable, principal):
                        calls['count'] += 1
                        value = original_snapshot(durable, principal)
                        # Every request is expected to need exactly two
                        # projections: force an independent SQLite writer to
                        # advance the first cut, then leave the retry stable.
                        if calls['count'] % 2 == 1:
                            sequence = (calls['count'] + 1) // 2
                            payload = {
                                'reason': 'product-state-repeated-concurrent-race',
                                'request': sequence,
                            }
                            future = writers.submit(
                                writer.append_event,
                                {
                                    'event_id': f'product-state-repeated-race-{sequence}',
                                    'event_type': 'ProductStateRaceInjected',
                                    'aggregate_type': 'product_state_race_test',
                                    'aggregate_id': f'repeated-{sequence}',
                                    'aggregate_version': '1',
                                    'payload': payload,
                                    'payload_hash': payload_digest(payload),
                                    'committed_at': '2026-10-04T00:00:00Z',
                                },
                            )
                            future.result(timeout=5)
                        return value

                    with patch.object(
                        client.runtime.application,
                        '_snapshot_provider',
                        first_attempt_race,
                    ):
                        for _ in range(requests):
                            status, state, _ = client.request('GET', '/api/v1/state')
                            self.assertEqual(status, 200)
                            self.assertEqual(
                                state['reason_codes'],
                                ['SIMULATION_ONLY', 'ECONOMIC_EDGE_UNPROVEN'],
                            )
                self.assertEqual(calls['count'], requests * 2)
            finally:
                client.close()

    def test_host_backpressure_rejects_before_admission_and_keeps_retry_identity(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                snapshot = client.state()
                identity = str(uuid4())
                command = {'command_id': identity, 'idempotency_key': identity,
                    'expected_state_version': snapshot['state_version'], 'actor': 'local-owner',
                    'session': snapshot['permission_summary']['session'], 'account_id': ACCOUNT,
                    'environment': ENVIRONMENT, 'action': 'START_SIMULATION', 'payload': {}}
                before = client.runtime.journal.current_journal_sequence()
                slots = client.runtime.server._request_slots
                acquired = 0
                try:
                    # Deterministically hold the real server's request capacity,
                    # rather than depend on machine timing to force saturation.
                    while slots.acquire(blocking=False): acquired += 1
                    for _ in range(40):
                        status, result, _ = client.request('POST', '/api/v1/commands', command)
                        self.assertEqual(status, 503)
                        self.assertEqual(result, {'error': 'HOST_OVERLOADED', 'accepted': False})
                    self.assertEqual(client.runtime.journal.current_journal_sequence(), before)
                finally:
                    for _ in range(acquired): slots.release()
                status, accepted, _ = client.request('POST', '/api/v1/commands', command)
                self.assertEqual((status, accepted['status']), (200, 'ACCEPTED'))
                operation_id = accepted['operation_id']
                for _ in range(300):
                    _, operation, _ = client.request('GET', '/api/v1/operations/' + operation_id)
                    if operation['phase'] == 'SUCCEEDED': break
                    time.sleep(.02)
                self.assertEqual(operation['phase'], 'SUCCEEDED')
                status, replay, _ = client.request('POST', '/api/v1/commands', command)
                self.assertEqual(replay['operation_id'], operation_id)
                self.assertEqual(len(client.state()['portfolio']['fills']), 6)
                self.assertEqual(client.state()['portfolio']['status']['cash'], '895.696')
            finally: client.close()

    def test_historical_admission_replay_cannot_authorize_stale_current_cash(self):
        from mvp.autotrade_mvp.reconciliation_journal import load_account_resource_availability_evidence
        from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation
        with TemporaryDirectory() as directory:
            run_autonomous_simulation(['100', '101', '103', '102', '100'], directory,
                run_id='historical-cut', now='2026-10-04T00:00:00Z', execution_profile='TWO_EQUAL_PARTIALS', target_quantity='2')
            journal = JournalStore(Path(directory) / 'journal.sqlite3')
            risk = journal.load_events_by_aggregate_type('risk_decision')[0]
            evidence = risk['payload']['reservation_availability_evidence']
            args = dict(checkpoint_event_id=evidence['checkpoint_event_id'], provider_id=PROVIDER,
                account_id=ACCOUNT, environment=ENVIRONMENT, resources=['CASH:USD'],
                now=risk['payload']['evaluated_at'], max_age_seconds='60')
            with self.assertRaisesRegex(ValueError, 'predates settlement financial truth'):
                load_account_resource_availability_evidence(journal, **args)
            historical = load_account_resource_availability_evidence(
                journal, **args, journal_sequence_cut=risk['journal_sequence']
            )
            self.assertEqual(historical['availability'], evidence['availability'])
            with self.assertRaisesRegex(ValueError, 'cannot use a historical journal cut'):
                load_account_resource_availability_evidence(
                    journal, **args, journal_sequence_cut=risk['journal_sequence'],
                    require_latest_scope=True
                )
            checkpoint = journal.get_event(evidence['checkpoint_event_id'])
            with self.assertRaisesRegex(ValueError, 'checkpoint is after'):
                load_account_resource_availability_evidence(
                    journal, **args,
                    journal_sequence_cut=checkpoint['journal_sequence'] - 1,
                )

    def test_whole_application_partial_fill_crash_restart_backup_restore_interface(self):
        with TemporaryDirectory() as directory:
            data = Path(directory) / 'product'
            client = ProductClient(data)
            try:
                status, html, _ = client.request('GET', '/')
                self.assertEqual(status, 200)
                self.assertIn('Start provider-free simulation', html)
                state = client.state()
                self.assertEqual(state['environment'], 'SIMULATION')
                self.assertEqual(state['jobs'][0]['result']['status'], 'DIAGNOSTIC_ONLY')
                self.assertTrue(state['jobs'][0]['result']['proposals'])
            finally: client.close()

            # Kill the worker process after the first real atomic half-fill. The
            # canonical provider observation is already durable; no state is
            # invented in the parent test process.
            script = '''
import os, sys
from pathlib import Path
import mvp.autotrade_mvp.simulation_session as session
from mvp.autotrade_mvp.simulation_commands import _protocol
from mvp.autotrade_mvp.persistence import JournalStore
root=Path(sys.argv[1]); p=_protocol(JournalStore(root/'journal.sqlite3'))
original=session.commit_order_fill_with_reservation_consumption
def crash(*a, **kw):
    value=original(*a, **kw)
    os._exit(73)
session.commit_order_fill_with_reservation_consumption=crash
session.run_autonomous_simulation(p['prices'],root,run_id=p['run_id'],now=p['start_time'],execution_profile='TWO_EQUAL_PARTIALS')
'''
            crashed = subprocess.run([sys.executable, '-c', script, str(data / 'state')], cwd=ROOT,
                capture_output=True, timeout=30)
            self.assertEqual(crashed.returncode, 73, crashed.stderr.decode())
            store = JournalStore(data / 'state' / 'journal.sqlite3')
            book = DurableProviderEconomicBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT)
            self.assertEqual(str(book.position(INSTRUMENT)), '0.5')
            oms = DurableOrderBookProjection(store, provider_id=PROVIDER, account_id=ACCOUNT,
                environment=ENVIRONMENT, host_id='local-simulation', owner_epoch='1')
            self.assertEqual(oms.snapshots[0].state, 'PARTIALLY_FILLED')

            client = ProductClient(data)
            try:
                self.assertEqual(client.state()['portfolio']['status']['session_status'], 'UNKNOWN')

                # START is ordinary forward execution, not recovery authority.
                # An unresolved durable episode must require the explicit
                # RECOVER_SIMULATION action before any recovery worker can run.
                before_rejected_start = client.state()
                rejected_id = str(uuid4())
                rejected_request = {
                    'command_id': rejected_id,
                    'idempotency_key': rejected_id,
                    'expected_state_version': before_rejected_start['state_version'],
                    'actor': 'local-owner',
                    'session': before_rejected_start['permission_summary']['session'],
                    'account_id': ACCOUNT,
                    'environment': ENVIRONMENT,
                    'action': 'START_SIMULATION',
                    'payload': {},
                }
                rejected_status, rejected, _ = client.request(
                    'POST', '/api/v1/commands', rejected_request
                )
                self.assertEqual(rejected_status, 400)
                self.assertEqual(rejected, {'error': 'INVALID_REQUEST'})
                after_rejected_start = client.state()
                self.assertEqual(
                    after_rejected_start['state_version'],
                    before_rejected_start['state_version'],
                )
                self.assertEqual(
                    after_rejected_start['portfolio']['status']['session_status'],
                    'UNKNOWN',
                )
                self.assertFalse(any(
                    event['event_type'] == 'COMMAND_ACCEPTED'
                    and event['payload'].get('command_id') == rejected_id
                    for event in store.load_events_by_aggregate_type('HOST_CONTROL')
                ))

                _, operation = client.command('RECOVER_SIMULATION')
                self.assertEqual(operation['phase'], 'SUCCEEDED', operation)
                state = client.state()
                self.assertEqual(state['portfolio']['status']['session_status'], 'COMPLETED')
                self.assertEqual(state['portfolio']['status']['cash'], '895.696')
                self.assertEqual(state['portfolio']['status']['position'], '1')
                self.assertEqual(len(state['portfolio']['fills']), 6)
                self.assertEqual([o['state'] for o in state['portfolio']['orders']], ['FILLED'] * 3)
                self.assertEqual(len(store.load_events_by_aggregate_type('settlement_book')), 12)
                before = store.current_journal_sequence()
                _, replay = client.command('START_SIMULATION')
                self.assertEqual(replay['phase'], 'SUCCEEDED')
                self.assertEqual(len(client.state()['portfolio']['fills']), 6)
                backup_id, operation = client.command('BACKUP_SIMULATION')
                self.assertEqual(operation['phase'], 'SUCCEEDED', operation)
                self.assertGreater(store.current_journal_sequence(), before)
            finally: client.close()

            backup = data / 'backups' / backup_id
            verify_backup(backup)
            restored = Path(directory) / 'restored'
            restore_product_backup(backup, restored)
            self.assertTrue(restore_requires_reconciliation(restored))
            client = ProductClient(restored)
            try:
                state = client.state()
                self.assertEqual(state['portfolio']['status']['cash'], '895.696')
                self.assertEqual(len(state['portfolio']['fills']), 6)
                self.assertEqual(state['risk']['real_order_submission'], 'UNAVAILABLE')
                self.assertEqual(state['risk']['restore_trading_gate'], 'RECONCILIATION_REQUIRED')
                _, operation = client.command('RECOVER_SIMULATION')
                self.assertEqual(operation['phase'], 'SUCCEEDED', operation)
                self.assertEqual(client.state()['portfolio']['status']['cash'], '895.696')
            finally: client.close()

    def test_product_restore_layout_failure_leaves_final_destination_retryable(self):
        with TemporaryDirectory() as directory:
            data = Path(directory) / 'product'
            client = ProductClient(data)
            try:
                _, simulation = client.command('START_SIMULATION')
                self.assertEqual(simulation['phase'], 'SUCCEEDED', simulation)
                backup_id, operation = client.command('BACKUP_SIMULATION')
                self.assertEqual(operation['phase'], 'SUCCEEDED', operation)
            finally:
                client.close()

            backup = data / 'backups' / backup_id
            restored = Path(directory) / 'retryable-restored'
            original_rename = Path.rename

            def fail_zero_layout(path, target):
                if Path(path).name == 'artifacts' and Path(target).name == 'artifacts':
                    raise OSError('simulated ZERO artifact layout failure')
                return original_rename(path, target)

            with patch.object(Path, 'rename', new=fail_zero_layout):
                with self.assertRaisesRegex(
                    OSError,
                    'simulated ZERO artifact layout failure',
                ):
                    restore_product_backup(backup, restored)

            self.assertFalse(restored.exists())
            result = restore_product_backup(backup, restored)
            self.assertEqual(result, restored)
            self.assertTrue((restored / 'state' / 'artifacts').is_dir())
            self.assertFalse((restored / 'artifacts').exists())
            self.assertTrue(restore_requires_reconciliation(restored))

    def test_worker_pause_allows_exact_durable_block_restore(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / 'journal.sqlite3')
            authority = AuthorityService(store)
            blocked_at = '2026-10-04T00:00:00Z'
            block_reason = 'host_operator_command:BLOCK_NEW_EXPOSURE:EMERGENCY_STOP'
            authority.block_new_exposure(
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                reason=block_reason,
                blocked_at=blocked_at,
                command_id='block-command-1',
            )
            host_events = [
                {'event_type': 'COMMAND_ACCEPTED',
                 'payload': {'action': 'BLOCK_NEW_EXPOSURE',
                             'operation_id': 'block-operation-1'}},
                {'event_type': 'OPERATION_UPDATED',
                 'payload': {'operation_id': 'block-operation-1',
                             'phase': 'RUNNING'}},
                {'event_type': 'OPERATION_UPDATED',
                 'payload': {'operation_id': 'block-operation-1',
                             'phase': 'SUCCEEDED'}},
            ]
            def append_host_event(item):
                # Use durable events rather than a forbidden instance shadow.
                event_number = len(store.load_events('HOST_CONTROL', 'host-test')) + 1
                store.append_event({
                    'event_id': str(uuid4()),
                    'event_type': item['event_type'],
                    'aggregate_type': 'HOST_CONTROL',
                    'aggregate_id': 'host-test',
                    'aggregate_version': str(event_number),
                    'payload': item['payload'],
                    'payload_hash': payload_digest(item['payload']),
                    'committed_at': '2026-10-04T00:00:00Z',
                })

            for item in host_events:
                append_host_event(item)
            self.assertTrue(_host_emergency_pause_required(store))
            authority.restore_new_exposure(
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                reason='host_operator_command:SET_AUTHORITY:RESTORE_NEW_EXPOSURE:POLICY_REVIEW',
                restored_at='2026-10-04T00:00:01Z',
                command_id='restore-command-1',
                expected_block_command_id='block-command-1',
                expected_block_reason=block_reason,
                expected_blocked_at=blocked_at,
            )
            self.assertFalse(_host_emergency_pause_required(store))

            append_host_event({
                'event_type': 'COMMAND_ACCEPTED',
                'payload': {'action': 'BLOCK_NEW_EXPOSURE',
                            'operation_id': 'block-operation-2'},
            })
            self.assertTrue(_host_emergency_pause_required(store))
            append_host_event({
                'event_type': 'OPERATION_UPDATED',
                'payload': {'operation_id': 'block-operation-2', 'phase': 'FAILED'},
            })
            self.assertFalse(_host_emergency_pause_required(store))

            append_host_event({
                'event_type': 'COMMAND_ACCEPTED',
                'payload': {'action': 'REVOKE_AUTHORITY',
                            'operation_id': 'revoke-operation-1'},
            })
            self.assertTrue(_host_emergency_pause_required(store))

    def test_worker_rejects_unaccepted_lifecycle_invocation(self):
        with TemporaryDirectory() as directory:
            data = Path(directory) / 'product'
            client = ProductClient(data)
            try:
                store = client.runtime.journal
                before = store.current_journal_sequence()
            finally:
                client.close()

            forged_command_id = str(uuid4())
            worker = subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'mvp.autotrade_mvp.product_worker',
                    '--state-dir',
                    str(data / 'state'),
                    '--action',
                    'START_SIMULATION',
                    '--command-id',
                    forged_command_id,
                ],
                cwd=ROOT,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(worker.returncode, 0)
            self.assertEqual(
                JournalStore(data / 'state' / 'journal.sqlite3').current_journal_sequence(),
                before,
            )

    def test_simulation_receipt_replay_requires_exact_canonical_envelope(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                store = client.runtime.journal
                protocol = _protocol(store)
                state = client.state()['portfolio']['status']
                command_id = str(uuid4())
                payload = {
                    'schema_version': 1,
                    'command_id': command_id,
                    'account_id': ACCOUNT,
                    'environment': ENVIRONMENT,
                    'protocol_digest': payload_digest(protocol),
                    'stop_after_episodes': 1,
                }
                result = {
                    'status': 'PAUSED',
                    'completed_episodes': 1,
                    'cash': state['cash'],
                    'position': state['position'],
                    'protocol_digest': payload['protocol_digest'],
                    'economic_edge_status': 'INCONCLUSIVE',
                }
                body = {
                    'action': 'START_SIMULATION',
                    'command_payload_hash': payload_digest(payload),
                    'result': result,
                }
                timestamp = protocol['start_time']
                forged = {
                    'event_id': str(uuid4()),
                    'event_type': 'NotSimulationOperatorCompleted',
                    'schema_version': '1.0.0',
                    'aggregate_type': 'simulation_operator_receipt',
                    'aggregate_id': command_id,
                    'aggregate_version': '1',
                    'host_id': 'local-simulation',
                    'owner_epoch': '1',
                    'environment': 'SIMULATION',
                    'occurred_at': timestamp,
                    'observed_at': timestamp,
                    'committed_at': timestamp,
                    'correlation_id': command_id,
                    'causation_id': None,
                    'payload': body,
                    'payload_hash': payload_digest(body),
                    'evidence_refs': [],
                }
                store.append_event(forged)
                with self.assertRaisesRegex(
                    ValueError,
                    'receipt envelope identity differs',
                ):
                    resolve_simulation_action(
                        store,
                        'START_SIMULATION',
                        payload,
                    )
            finally:
                client.close()

    def test_simulation_receipt_is_cas_bound_to_verified_journal_cut(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            observed_cuts = []
            original_append = JournalStore.append_event

            def capture_receipt_cut(store, envelope, **kwargs):
                if envelope.get('aggregate_type') == 'simulation_operator_receipt':
                    observed_cuts.append(kwargs.get('expected_journal_sequence'))
                return original_append(store, envelope, **kwargs)

            try:
                with patch.object(JournalStore, 'append_event', new=capture_receipt_cut):
                    _command_id, operation = client.command('START_SIMULATION')
                self.assertEqual(operation['phase'], 'SUCCEEDED', operation)
                self.assertEqual(len(observed_cuts), 1)
                self.assertIs(type(observed_cuts[0]), int)
                self.assertGreater(observed_cuts[0], 0)

                events = client.runtime.journal.load_events_by_aggregate_type(
                    'simulation_operator_receipt'
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(
                    events[0]['journal_sequence'],
                    observed_cuts[0] + 1,
                )
            finally:
                client.close()

    def test_worker_stdout_cannot_forge_financial_completion(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                before = client.state()
                self.assertEqual(
                    before['portfolio']['status']['completed_episodes'],
                    1,
                )
                forged = subprocess.CompletedProcess(
                    args=['provider-free-worker'],
                    returncode=0,
                    stdout=json.dumps({
                        'status': 'COMPLETED',
                        'completed_episodes': 8,
                        'cash': '999999',
                        'position': '999999',
                        'protocol_digest': 'sha256:' + '0' * 64,
                        'economic_edge_status': 'PROVEN',
                    }).encode(),
                    stderr=b'',
                )
                with patch('subprocess.run', return_value=forged):
                    _command_id, operation = client.command('START_SIMULATION')
                    self.assertEqual(operation['phase'], 'UNKNOWN', operation)

                unchanged = client.state()
                self.assertEqual(
                    unchanged['portfolio']['status']['completed_episodes'],
                    1,
                )
                self.assertEqual(
                    unchanged['portfolio']['status']['cash'],
                    before['portfolio']['status']['cash'],
                )
                self.assertEqual(
                    unchanged['portfolio']['status']['position'],
                    before['portfolio']['status']['position'],
                )

                # Healthy recovery resolves the same accepted operation from
                # durable state; the forged stdout never becomes receipt truth.
                client.runtime.application.resume_authority_operations()
                status, recovered, _ = client.request(
                    'GET',
                    '/api/v1/operations/' + operation['operation_id'],
                )
                self.assertEqual(status, 200)
                self.assertEqual(recovered['phase'], 'SUCCEEDED', recovered)
            finally:
                client.close()

    def test_worker_timeout_remains_resumable_without_poisoning_host_recovery(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                with patch(
                    'subprocess.run',
                    side_effect=subprocess.TimeoutExpired(
                        cmd=['provider-free-worker'],
                        timeout=300,
                    ),
                ):
                    _command_id, operation = client.command('START_SIMULATION')
                    self.assertEqual(operation['phase'], 'UNKNOWN', operation)
                    self.assertEqual(
                        operation['remaining_uncertainty'],
                        ['authority_execution_fault'],
                    )
                    self.assertTrue(any(
                        item.get('kind') == 'authority-execution-fault'
                        for item in operation['evidence']
                    ))
                    # A second startup-equivalent recovery attempt may fail for
                    # the same external reason. It must preserve UNKNOWN rather
                    # than attempt an illegal UNKNOWN -> UNKNOWN journal write.
                    resumed = client.runtime.application.resume_authority_operations()
                    self.assertIn(operation['operation_id'], resumed)
                    status, repeated, _ = client.request(
                        'GET',
                        '/api/v1/operations/' + operation['operation_id'],
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(repeated['phase'], 'UNKNOWN')
                    self.assertEqual(
                        repeated['remaining_uncertainty'],
                        ['authority_execution_fault'],
                    )

                # Once the worker is available again, resume the same accepted
                # durable operation. No replacement command or resend identity
                # is fabricated.
                resumed = client.runtime.application.resume_authority_operations()
                self.assertIn(operation['operation_id'], resumed)
                status, recovered, _ = client.request(
                    'GET',
                    '/api/v1/operations/' + operation['operation_id'],
                )
                self.assertEqual(status, 200)
                self.assertEqual(recovered['phase'], 'SUCCEEDED', recovered)
                self.assertEqual(recovered['remaining_uncertainty'], [])
            finally:
                client.close()


    def test_browser_keeps_retryable_pairing_fragment_until_pairing_succeeds(self):
        app = (ROOT / 'web' / 'src' / 'app.js').read_text(encoding='utf-8')
        pairing = 'await jsonFetch("api/v1/session", {'
        scrub = 'window.history.replaceState(null, "", window.location.pathname);'
        self.assertIn(pairing, app)
        self.assertIn(scrub, app)
        self.assertLess(app.index(pairing), app.index(scrub))


    def test_default_launcher_message_does_not_emit_pairing_secret(self):
        launch_url = 'http://127.0.0.1:8765/#pair=one-time-owner-secret'
        message = _launch_message(launch_url, no_browser=False)
        self.assertIn('http://127.0.0.1:8765/', message)
        self.assertNotIn('one-time-owner-secret', message)
        self.assertNotIn('#pair=', message)
        self.assertIn('opening the local browser', message)

    def test_no_browser_launcher_explicitly_emits_one_time_manual_pairing_url(self):
        launch_url = 'http://127.0.0.1:8765/#pair=manual-owner-secret'
        message = _launch_message(launch_url, no_browser=True)
        self.assertIn('one-time local secret', message)
        self.assertIn(launch_url, message)

    def test_browser_open_failure_has_explicit_manual_pairing_fallback(self):
        runtime_source = (
            ROOT / 'mvp' / 'autotrade_mvp' / 'product_runtime.py'
        ).read_text(encoding='utf-8')
        self.assertIn('if not webbrowser.open(launch_url):', runtime_source)
        self.assertIn(
            'print(_launch_message(launch_url, no_browser=True), flush=True)',
            runtime_source,
        )

    def test_launcher_message_rejects_missing_pairing_material(self):
        with self.assertRaisesRegex(ValueError, 'pairing material'):
            _launch_message('http://127.0.0.1:8765/', no_browser=False)

    def test_pairing_exports_exact_browser_session_to_native_sink(self):
        with TemporaryDirectory() as directory:
            captured = []

            def sink(**values):
                captured.append(values)

            client = ProductClient(directory, desktop_session_sink=sink)
            try:
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]['origin'], client.origin)
                self.assertEqual(captured[0]['actor'], 'local-owner')
                self.assertEqual(
                    client.cookie,
                    'AutoTradeSession=' + captured[0]['token'],
                )
            finally:
                client.close()

    def test_native_header_client_uses_same_paired_owner_session(self):
        with TemporaryDirectory() as directory:
            captured = []
            client = ProductClient(
                directory,
                desktop_session_sink=lambda **values: captured.append(values),
            )
            try:
                self.assertEqual(len(captured), 1)
                token = captured[0]['token']
                browser_state = client.state()

                connection = http.client.HTTPConnection(
                    '127.0.0.1',
                    client.port,
                    timeout=30,
                )
                try:
                    connection.request(
                        'GET',
                        '/api/v1/state',
                        headers={
                            'Origin': client.origin,
                            'Authorization': 'AutoTrade-Session ' + token,
                            'X-AutoTrade-Actor': 'local-owner',
                        },
                    )
                    response = connection.getresponse()
                    native_state = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                finally:
                    connection.close()

                self.assertEqual(
                    native_state['permission_summary']['session'],
                    browser_state['permission_summary']['session'],
                )
                self.assertEqual(
                    native_state['permission_summary']['actor'],
                    'local-owner',
                )

                connection = http.client.HTTPConnection(
                    '127.0.0.1',
                    client.port,
                    timeout=30,
                )
                try:
                    connection.request(
                        'GET',
                        '/api/v1/state',
                        headers={
                            'Origin': client.origin,
                            'Authorization': 'AutoTrade-Session ' + token,
                            'X-AutoTrade-Actor': 'wrong-actor',
                        },
                    )
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 403)
                finally:
                    connection.close()
            finally:
                client.close()

    def test_paired_owner_session_preserves_canonical_short_rolling_idle_window(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            boundary = client.runtime.application._boundary
            try:
                token = client.cookie.split('=', 1)[1]
                issued = boundary._sessions[token]
                issued_at = issued.expires_at - 3600
                self.assertEqual(issued.idle_timeout_seconds, 300)
                self.assertEqual(issued.idle_expires_at, issued_at + 300)
                self.assertEqual(issued.expires_at, issued_at + 3600)

                boundary._now = lambda: issued_at + 299
                status, state, _ = client.request('GET', '/api/v1/state')
                self.assertEqual(status, 200)
                self.assertEqual(state['environment'], 'SIMULATION')
                refreshed = boundary._sessions[token]
                self.assertEqual(refreshed.idle_timeout_seconds, 300)
                self.assertEqual(refreshed.idle_expires_at, issued_at + 599)
                self.assertEqual(refreshed.expires_at, issued_at + 3600)

                boundary._now = lambda: issued_at + 599
                status, body, _ = client.request('GET', '/api/v1/state')
                self.assertEqual(status, 403)
                self.assertEqual(body, {'error': 'AUTHENTICATION_OR_AUTHORIZATION_FAILED'})
            finally:
                client.close()

    def test_duplicate_session_cookie_names_fail_closed_without_revoking_valid_session(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                connection = http.client.HTTPConnection(
                    '127.0.0.1',
                    client.port,
                    timeout=30,
                )
                try:
                    connection.request(
                        'GET',
                        '/api/v1/state',
                        headers={
                            'Origin': client.origin,
                            'Cookie': client.cookie + '; AutoTradeSession=attacker-shadow',
                        },
                    )
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    self.assertEqual(response.status, 403)
                    self.assertEqual(body, {'error': 'AUTHENTICATION_OR_AUTHORIZATION_FAILED'})
                finally:
                    connection.close()

                self.assertEqual(client.state()['environment'], 'SIMULATION')
            finally:
                client.close()

    def test_pairing_code_is_consumed_by_exactly_one_concurrent_claimant(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory, auto_pair=False)
            boundary = client.runtime.application._boundary
            create_session = boundary.create_session

            def slow_create_session(*args, **kwargs):
                # Widen the pre-fix check/create race. Without serialized
                # consumption both request threads can mint an OWNER session
                # from the same one-time code.
                time.sleep(.15)
                return create_session(*args, **kwargs)

            def claim(_index):
                return client.request(
                    'POST',
                    '/api/v1/session',
                    {'pairing_code': client.pairing_code},
                )

            try:
                with patch.object(
                    boundary,
                    'create_session',
                    side_effect=slow_create_session,
                ), ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(claim, range(2)))
                self.assertEqual(
                    sorted(status for status, _payload, _headers in results),
                    [200, 403],
                )
                issued = [
                    headers.get('Set-Cookie')
                    for status, _payload, headers in results
                    if status == 200
                ]
                self.assertEqual(len(issued), 1)
                self.assertIn('AutoTradeSession=', issued[0])
            finally:
                client.close()

    def test_pairing_store_failure_revokes_token_and_allows_exact_code_retry(self):
        with TemporaryDirectory() as directory:
            captured = []

            def fail_once(**values):
                captured.append(values)
                if len(captured) == 1:
                    raise OSError('simulated credential-manager failure')

            client = ProductClient(
                directory,
                desktop_session_sink=fail_once,
                auto_pair=False,
            )
            try:
                status, payload, _ = client.request(
                    'POST',
                    '/api/v1/session',
                    {'pairing_code': client.pairing_code},
                )
                self.assertEqual(status, 503)
                self.assertEqual(payload['error'], 'DESKTOP_SESSION_STORE_FAILED')

                status, payload, headers = client.request(
                    'POST',
                    '/api/v1/session',
                    {'pairing_code': client.pairing_code},
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload['status'], 'PAIRED')
                client.cookie = headers['Set-Cookie'].split(';')[0]
                self.assertEqual(len(captured), 2)
                self.assertNotEqual(captured[0]['token'], captured[1]['token'])
                self.assertEqual(
                    client.cookie,
                    'AutoTradeSession=' + captured[1]['token'],
                )
            finally:
                client.close()

    def test_desktop_session_target_matches_native_contract(self):
        self.assertEqual(
            desktop_session_credential_target('http://127.0.0.1:8765'),
            'AutoTrade.HostSession:http://127.0.0.1:8765',
        )
        for origin in (
            'https://127.0.0.1:8765',
            'http://localhost:8765',
            'http://127.0.0.1:8765/',
            'http://127.0.0.1',
            'http://127.0.0.1:8765/path',
        ):
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    desktop_session_credential_target(origin)

    def test_desktop_bearer_is_scoped_to_current_windows_logon_session(self):
        module = (
            ROOT / 'mvp' / 'autotrade_mvp' / 'windows_host_session.py'
        ).read_text(encoding='utf-8')
        self.assertIn('_CREDENTIAL_PERSIST_SESSION = 1', module)
        self.assertIn('Persist=_CREDENTIAL_PERSIST_SESSION', module)
        self.assertNotIn('_CREDENTIAL_PERSIST_LOCAL_MACHINE', module)

    def test_windows_session_persistence_passes_exact_owner_material_to_os_writer(self):
        with patch(
            'mvp.autotrade_mvp.windows_host_session._running_on_windows',
            return_value=True,
        ), patch(
            'mvp.autotrade_mvp.windows_host_session._write_windows_generic_credential'
        ) as writer:
            target = persist_desktop_owner_session(
                origin='http://127.0.0.1:8765',
                actor='local-owner',
                token='session-token-123',
            )
        self.assertEqual(
            target,
            'AutoTrade.HostSession:http://127.0.0.1:8765',
        )
        writer.assert_called_once_with(
            target='AutoTrade.HostSession:http://127.0.0.1:8765',
            actor='local-owner',
            token='session-token-123',
        )

    def test_installed_bundle_source_revision_does_not_require_git_checkout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bundle-manifest.json').write_text(
                json.dumps({'source_sha': 'a' * 40}),
                encoding='utf-8',
            )
            with patch(
                'mvp.autotrade_mvp.product_runtime.ROOT',
                root,
            ), patch(
                'mvp.autotrade_mvp.product_runtime.subprocess.check_output',
                side_effect=AssertionError('Git must not be consulted'),
            ):
                self.assertEqual(source_revision(), 'a' * 40)

    def test_matching_installed_source_revision_authorities_are_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            (root / 'SOURCE_REVISION').write_text(revision + '\n', encoding='utf-8')
            (root / 'bundle-manifest.json').write_text(
                json.dumps({'source_sha': revision}),
                encoding='utf-8',
            )
            with patch(
                'mvp.autotrade_mvp.product_runtime.ROOT',
                root,
            ), patch(
                'mvp.autotrade_mvp.product_runtime.subprocess.check_output',
                side_effect=AssertionError('Git must not be consulted'),
            ):
                self.assertEqual(source_revision(), revision)

    def test_installed_source_revision_authorities_must_agree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'SOURCE_REVISION').write_text('a' * 40, encoding='utf-8')
            (root / 'bundle-manifest.json').write_text(
                json.dumps({'source_sha': 'b' * 40}),
                encoding='utf-8',
            )
            with patch(
                'mvp.autotrade_mvp.product_runtime.ROOT',
                root,
            ), patch(
                'mvp.autotrade_mvp.product_runtime.subprocess.check_output',
                side_effect=AssertionError('Git must not mask installed identity conflict'),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    'source revision authorities disagree',
                ):
                    source_revision()

    def test_duplicate_installed_bundle_source_sha_fails_closed_before_git(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bundle-manifest.json').write_text(
                '{"source_sha":"' + ('a' * 40) + '","source_sha":"' + ('b' * 40) + '"}',
                encoding='utf-8',
            )
            with patch(
                'mvp.autotrade_mvp.product_runtime.ROOT',
                root,
            ), patch(
                'mvp.autotrade_mvp.product_runtime.subprocess.check_output',
                side_effect=AssertionError('Git must not mask ambiguous bundle identity'),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    'not strict JSON',
                ):
                    source_revision()

    def test_invalid_installed_bundle_source_revision_fails_closed_before_git(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bundle-manifest.json').write_text(
                json.dumps({'source_sha': 'A' * 40}),
                encoding='utf-8',
            )
            with patch(
                'mvp.autotrade_mvp.product_runtime.ROOT',
                root,
            ), patch(
                'mvp.autotrade_mvp.product_runtime.subprocess.check_output',
                side_effect=AssertionError('Git must not mask invalid bundle identity'),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    'bundle manifest source revision',
                ):
                    source_revision()

    def test_second_host_cannot_own_same_product(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                with self.assertRaisesRegex(RuntimeError, 'already owned'):
                    build_product(directory, port=client.port + 1)
            finally: client.close()

    def test_duplicate_cookie_headers_fail_closed_before_session_resolution(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                connection = http.client.HTTPConnection(
                    '127.0.0.1',
                    client.port,
                    timeout=30,
                )
                try:
                    connection.putrequest('GET', '/api/v1/state')
                    connection.putheader('Origin', client.origin)
                    connection.putheader('Cookie', client.cookie)
                    connection.putheader('Cookie', client.cookie)
                    connection.endheaders()
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    self.assertEqual(response.status, 400)
                    self.assertEqual(body, {'error': 'INVALID_REQUEST'})
                finally:
                    connection.close()

                # The unambiguous cookie remains valid after the rejected request.
                self.assertEqual(client.state()['environment'], 'SIMULATION')
            finally:
                client.close()

    def test_pairing_rejects_duplicate_json_keys_without_consuming_code(self):
        with TemporaryDirectory() as directory:
            probe = socket.socket(); probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]; probe.close()
            origin = f'http://127.0.0.1:{port}'
            runtime, url = build_product(
                directory,
                port=port,
                desktop_session_sink=lambda **_kwargs: None,
            )
            worker = Thread(target=runtime.serve_forever)
            worker.start()
            pairing_code = url.split('#pair=')[1]
            try:
                body = (
                    '{"pairing_code":"attacker-shadow","pairing_code":'
                    + json.dumps(pairing_code)
                    + '}'
                )
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
                try:
                    connection.request(
                        'POST',
                        '/api/v1/session',
                        body,
                        {'Origin': origin, 'Content-Type': 'application/json'},
                    )
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 403)
                    self.assertIsNone(response.getheader('Set-Cookie'))
                finally:
                    connection.close()

                # Rejection must not consume the one-time code.
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
                try:
                    valid = json.dumps({'pairing_code': pairing_code})
                    connection.request(
                        'POST',
                        '/api/v1/session',
                        valid,
                        {'Origin': origin, 'Content-Type': 'application/json'},
                    )
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 200)
                    self.assertIn('AutoTradeSession=', response.getheader('Set-Cookie'))
                finally:
                    connection.close()
            finally:
                runtime.close()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())

    def test_pairing_and_commands_fail_closed_without_owner_session(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                client.cookie = None
                self.assertEqual(client.request('GET', '/api/v1/state')[0], 403)
                self.assertEqual(client.request('POST', '/api/v1/session', {'pairing_code': 'x'*43})[0], 403)
            finally: client.close()


if __name__ == '__main__': unittest.main()
