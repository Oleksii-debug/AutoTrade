"""Whole-product acceptance through the real HTTP interface and a killed worker.

This is provider-free process/UI-API evidence, not Windows/NVDA qualification.
"""
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
from uuid import uuid4

from mvp.autotrade_mvp.product_runtime import build_product, restore_product_backup
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulation_session import ACCOUNT, ENVIRONMENT, PROVIDER, INSTRUMENT
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.backup import verify_backup, restore_requires_reconciliation

ROOT = Path(__file__).resolve().parents[2]


class ProductClient:
    def __init__(self, directory):
        probe = socket.socket(); probe.bind(('127.0.0.1', 0))
        self.port = probe.getsockname()[1]; probe.close()
        self.origin = f'http://127.0.0.1:{self.port}'
        self.runtime, url = build_product(directory, port=self.port)
        self.worker = Thread(target=self.runtime.serve_forever)
        self.worker.start()
        self.cookie = None
        response, data, headers = self.request('POST', '/api/v1/session', {'pairing_code': url.split('#pair=')[1]})
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
            if status == 200: return data
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
                run_id='historical-cut', now='2026-10-04T00:00:00Z', partial_fills=True)
            journal = JournalStore(Path(directory) / 'journal.sqlite3')
            risk = journal.load_events_by_aggregate_type('risk_decision')[0]
            evidence = risk['payload']['reservation_availability_evidence']
            args = dict(checkpoint_event_id=evidence['checkpoint_event_id'], provider_id=PROVIDER,
                account_id=ACCOUNT, environment=ENVIRONMENT, resources=['CASH:USD'],
                now=risk['payload']['evaluated_at'], max_age_seconds='60')
            with self.assertRaisesRegex(ValueError, 'predates settlement financial truth'):
                load_account_resource_availability_evidence(journal, **args)
            historical = load_account_resource_availability_evidence(journal, **args,
                _historical_risk_event_id=risk['event_id'])
            self.assertEqual(historical['availability'], evidence['availability'])
            with self.assertRaisesRegex(ValueError, 'cannot authorize current'):
                load_account_resource_availability_evidence(journal, **args,
                    _historical_risk_event_id=risk['event_id'], require_latest_scope=True)
            with self.assertRaisesRegex(ValueError, 'durable risk event'):
                load_account_resource_availability_evidence(journal, **args,
                    _historical_risk_event_id=evidence['checkpoint_event_id'])

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
session.run_autonomous_simulation(p['prices'],root,run_id=p['run_id'],now=p['start_time'],partial_fills=True)
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

    def test_second_host_cannot_own_same_product(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                with self.assertRaisesRegex(RuntimeError, 'already owned'):
                    build_product(directory, port=client.port + 1)
            finally: client.close()

    def test_pairing_and_commands_fail_closed_without_owner_session(self):
        with TemporaryDirectory() as directory:
            client = ProductClient(directory)
            try:
                client.cookie = None
                self.assertEqual(client.request('GET', '/api/v1/state')[0], 403)
                self.assertEqual(client.request('POST', '/api/v1/session', {'pairing_code': 'x'*43})[0], 403)
            finally: client.close()


if __name__ == '__main__': unittest.main()
