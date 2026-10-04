"""One runnable provider-free product composed from the existing host and ZERO loop.

This launcher owns composition, not financial logic. All operator commands,
orders, fills, reservations and accounting use one canonical JournalStore.
"""
import argparse
from functools import partial
from hashlib import sha256
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
from threading import Thread
from urllib.parse import urlsplit
import webbrowser

from .embedded_web import EmbeddedWebHostApplication, ImmutableWebAsset, ImmutableWebAssetBundle, HOST_API_CONTRACT_VERSION
from .host_network import HostPrincipal, TransportResponse, public_session_reference, header_principal_resolver
from .production_host import ProductionHostConfig, build_production_host
from .security import SecurityBoundary
from .simulation_session import ACCOUNT, ENVIRONMENT, run_autonomous_simulation
from .simulation_status import inspect_canonical_simulation
from .simulation_commands import _protocol
from .windows_secrets import DpapiCurrentUserProtector, ProtectedCredentialVault

ROOT = Path(__file__).resolve().parents[2]
PRICES = ['100', '101', '103', '102', '100', '100', '101', '103']
START_TIME = '2026-10-04T00:00:00Z'


class _NoProviderSecrets:
    def protect(self, *args, **kwargs):
        raise PermissionError('provider credentials are unavailable in ZERO')
    def unprotect(self, *args, **kwargs):
        raise PermissionError('provider credentials are unavailable in ZERO')


def source_revision():
    marker = ROOT / 'SOURCE_REVISION'
    if marker.is_file():
        return marker.read_text().strip()
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()


def web_bundle():
    assets = []
    types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'}
    for name in ('index.html', 'app.js', 'host-api-routes.js', 'styles.css'):
        body = (ROOT / 'web' / 'src' / name).read_bytes()
        assets.append(ImmutableWebAsset(name, body, sha256(body).hexdigest(), types[Path(name).suffix]))
    return ImmutableWebAssetBundle(source_revision(), HOST_API_CONTRACT_VERSION, tuple(assets))


class ProviderFreeApplication(EmbeddedWebHostApplication):
    def __init__(self, journal, *, state_dir, pairing_code, **kwargs):
        self.state_dir = state_dir
        self._pairing_code = pairing_code
        # Bootstrap only one no-trade observation before command admission. The
        # empty-journal ownership rule remains intact and no order is sent.
        if not journal.load_events_by_aggregate_type('canonical_autonomous_simulation'):
            run_autonomous_simulation(PRICES, state_dir, run_id='provider-free-product',
                now=START_TIME, stop_after_episodes=1, partial_fills=True)
        else:
            _protocol(journal)
            # Structural/status verification is read-only on restart. Recovery is
            # an explicit durable operator command, never a silent resend.
            inspect_canonical_simulation(state_dir)
        from .product_research import prepare_research
        prepare_research(journal, _protocol(journal))
        self._origin = kwargs['public_origin']
        self._boundary = kwargs['security_boundary']
        super().__init__(journal, web_bundle=web_bundle(), **kwargs)

    def dispatch(self, *, method, target, headers, body=b''):
        if target == '/api/v1/session' and method == 'POST':
            normalized = {k.lower(): v for k, v in headers.items()}
            if (normalized.get('origin') != self._origin
                or normalized.get('content-type') != 'application/json'
                or len(body) > 256 or self._pairing_code is None):
                return TransportResponse(403, 'application/json', b'{"error":"PAIRING_REJECTED"}')
            try:
                request = json.loads(body)
                if set(request) != {'pairing_code'} or type(request['pairing_code']) is not str:
                    raise ValueError()
                if not secrets.compare_digest(request['pairing_code'], self._pairing_code):
                    raise ValueError()
                session = self._boundary.create_session(subject='local-owner', role='OWNER',
                    origin=self._origin, ttl_seconds=3600)
                self._pairing_code = None
                return TransportResponse(200, 'application/json', b'{"status":"PAIRED"}',
                    (('Set-Cookie', 'AutoTradeSession=' + session.token + '; HttpOnly; SameSite=Strict; Path=/api/v1; Max-Age=3600'),
                     ('Cache-Control', 'no-store')))
            except (ValueError, TypeError):
                return TransportResponse(403, 'application/json', b'{"error":"PAIRING_REJECTED"}')
        return super().dispatch(method=method, target=target, headers=headers, body=body)


def cookie_principal(headers, origin):
    del origin
    cookie = SimpleCookie()
    cookie.load(headers.get('cookie', ''))
    if 'AutoTradeSession' not in cookie:
        return header_principal_resolver(headers, 'local')
    token = cookie['AutoTradeSession'].value
    return HostPrincipal('local-owner', token, public_session_reference(token))


def build_product(data_dir, *, port=8765):
    data = Path(data_dir).resolve()
    state = data / 'state'
    state.mkdir(parents=True, exist_ok=True)
    origin = f'http://127.0.0.1:{port}'
    pairing = secrets.token_urlsafe(32)
    vault = ProtectedCredentialVault(data / 'credentials.json',
        protector=DpapiCurrentUserProtector() if os.name == 'nt' else _NoProviderSecrets())
    boundary = SecurityBoundary(allowed_origins={origin}, credential_vault=vault,
        session_authorizer=lambda subject, role, paired_origin:
            subject == 'local-owner' and role == 'OWNER' and paired_origin == origin)

    def snapshot(durable, principal):
        inspected = inspect_canonical_simulation(state, history_limit=100)
        status = inspected['status']
        report = inspected.get('economic_report')
        store = runtime.journal
        starts = store.load_events_by_aggregate_type('canonical_autonomous_simulation')
        completed = [e['payload'] for e in starts if e['event_type'] == 'AutonomousEpisodeCompleted']
        receipts = store.load_events_by_aggregate_type('simulation_operator_receipt')
        research = store.load_events_by_aggregate_type('provider_free_research')
        from .durable_order_projection import DurableOrderBookProjection
        from .simulation_session import PROVIDER
        oms = DurableOrderBookProjection(store, provider_id=PROVIDER, account_id=ACCOUNT,
            environment=ENVIRONMENT, host_id='local-simulation', owner_epoch='1')
        orders = [{'client_order_id': o.client_order_id, 'state': o.state,
            'requested_quantity': str(o.requested_quantity), 'filled_quantity': str(o.filled_quantity)}
            for o in oms.snapshots]
        fills = [{'fill_id': f.fill_id, 'quantity': str(f.quantity), 'price': str(f.price)} for f in oms.effective_fills()]
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        return {'state_version': durable['state_version'], 'event_cursor': durable['event_cursor'],
            'server_time': now, 'host_id': 'local-simulation', 'account_id': ACCOUNT, 'environment': ENVIRONMENT,
            'permission_summary': {'actor': principal.actor, 'session': principal.session, 'role': principal.role,
                'capabilities': ['START_SIMULATION', 'RECOVER_SIMULATION', 'BACKUP_SIMULATION', 'BLOCK_NEW_EXPOSURE']},
            'connection_freshness': {'host': 'CURRENT', 'as_of': now},
            'portfolio': {'status': status, 'economic_report': report, 'orders': orders, 'fills': fills},
            'risk': {'mode': 'ZERO', 'real_order_submission': 'UNAVAILABLE',
                'active_reservations': status.get('active_reservations', []),
                'restore_trading_gate': 'RECONCILIATION_REQUIRED' if (data / 'RESTORE_RECONCILIATION_REQUIRED.json').exists() else 'NOT_RESTORED'},
            'strategy': {'strategy': 'moving-average-2-3-long-only-target-1',
                'decisions': [{k: v for k, v in e.items() if k != 'provider_state'} for e in completed[-100:]],
                'economic_edge_status': 'INCONCLUSIVE'},
            'jobs': [{'kind': 'provider-free-research', 'result': e['payload']} for e in research] + [{'kind': 'provider-free-lifecycle', 'result': e['payload']} for e in receipts[-20:]],
            'reason_codes': ['SIMULATION_ONLY', 'ECONOMIC_EDGE_UNPROVEN']}

    config = ProductionHostConfig(state / 'journal.sqlite3', ACCOUNT, ENVIRONMENT,
        'local-simulation', '127.0.0.1', port, origin)
    runtime = build_production_host(config, security_boundary=boundary, principal_resolver=cookie_principal,
        snapshot_provider=snapshot, application_factory=partial(ProviderFreeApplication, state_dir=state, pairing_code=pairing))
    return runtime, origin + '/#pair=' + pairing


def restore_product_backup(backup_dir, data_dir):
    from .backup import restore_backup
    root = restore_backup(backup_dir, data_dir)
    # The shared backup format keeps artifacts alongside state. ZERO's existing
    # session expects state/artifacts. Move the verified directory atomically;
    # leave the original reconciliation/fencing gate untouched.
    source = root / 'artifacts'
    destination = root / 'state' / 'artifacts'
    if source.exists():
        if destination.exists():
            raise ValueError('restored artifact layout conflicts')
        source.rename(destination)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description='AutoTrade provider-free application')
    parser.add_argument('--data-dir', default=str(Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'AutoTrade-ZERO'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--restore-backup', help='Verify and restore a backup into a NEW data directory; real trading stays gated')
    args = parser.parse_args(argv)
    if args.restore_backup:
        restore_product_backup(args.restore_backup, args.data_dir)
    runtime, launch_url = build_product(args.data_dir, port=args.port)
    def stop(*_):
        Thread(target=runtime.close, daemon=True).start()
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, stop)
    print('AutoTrade ZERO: ' + launch_url, flush=True)
    if not args.no_browser:
        webbrowser.open(launch_url)
    try:
        runtime.serve_forever()
    finally:
        runtime.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
