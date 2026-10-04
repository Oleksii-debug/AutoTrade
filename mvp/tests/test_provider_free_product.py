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
from unittest.mock import patch
from uuid import uuid4

from mvp.autotrade_mvp.product_runtime import build_product, restore_product_backup, source_revision
from mvp.autotrade_mvp.windows_host_session import (
    desktop_session_credential_target,
    persist_desktop_owner_session,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulation_commands import _protocol, resolve_simulation_action
from mvp.autotrade_mvp.simulation_session import ACCOUNT, ENVIRONMENT, PROVIDER, INSTRUMENT
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
        pairing = 'await jsonFetch(HOST_API.route("pairLocalSession"), {'
        scrub = 'window.history.replaceState(null, "", window.location.pathname);'
        self.assertIn(pairing, app)
        self.assertIn(scrub, app)
        self.assertLess(app.index(pairing), app.index(scrub))

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
