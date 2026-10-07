"""One runnable provider-free product composed from the existing host and ZERO loop.

This launcher owns composition, not financial logic. All operator commands,
orders, fills, reservations and accounting use one canonical JournalStore.
"""
import argparse
import json
from functools import partial
from hashlib import sha256
from http.cookies import SimpleCookie
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
from threading import Lock, Thread
from urllib.parse import urlsplit
import webbrowser

from .embedded_web import EmbeddedWebHostApplication, ImmutableWebAsset, ImmutableWebAssetBundle, HOST_API_CONTRACT_VERSION
from .host_network import (
    HostPrincipal,
    SnapshotTemporarilyUnavailable,
    TransportResponse,
    _headers,
    public_session_reference,
    header_principal_resolver,
)
from .production_host import ProductionHostConfig, build_production_host
from .security import SecurityBoundary
from .simulation_session import ACCOUNT, ENVIRONMENT, run_autonomous_simulation
from .simulation_status import SimulationStateChanging, inspect_canonical_simulation
from .simulation_commands import _protocol
from .windows_secrets import DpapiCurrentUserProtector, ProtectedCredentialVault
from .windows_host_session import persist_desktop_owner_session

ROOT = Path(__file__).resolve().parents[2]
PRICES = ['100', '101', '103', '102', '100', '100', '101', '103']
START_TIME = '2026-10-04T00:00:00Z'


class _NoProviderSecrets:
    def protect(self, *args, **kwargs):
        raise PermissionError('provider credentials are unavailable in ZERO')
    def unprotect(self, *args, **kwargs):
        raise PermissionError('provider credentials are unavailable in ZERO')


def _canonical_source_revision(value, *, source):
    if (
        type(value) is not str
        or len(value) != 40
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise RuntimeError(f'{source} source revision must be exact lowercase 40-hex')
    return value


def source_revision():
    marker_revision = None
    marker = ROOT / 'SOURCE_REVISION'
    if marker.is_file():
        marker_revision = _canonical_source_revision(
            marker.read_text(encoding='utf-8').strip(),
            source='SOURCE_REVISION',
        )

    # Deterministic Windows bundles carry their exact source SHA in the bundle
    # manifest. If two installed identity authorities are present, require them
    # to agree instead of silently preferring a stale marker.
    bundle_revision = None
    bundle_manifest = ROOT / 'bundle-manifest.json'
    if bundle_manifest.is_file():
        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate bundle manifest JSON key')
                result[key] = value
            return result

        def reject_non_finite(constant):
            raise ValueError('non-finite bundle manifest JSON value: ' + constant)

        try:
            manifest = json.loads(
                bundle_manifest.read_text(encoding='utf-8'),
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_non_finite,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError('bundle source revision manifest is not strict JSON') from error
        if type(manifest) is not dict:
            raise RuntimeError('bundle source revision manifest must be an object')
        bundle_revision = _canonical_source_revision(
            manifest.get('source_sha'),
            source='bundle manifest',
        )

    if (
        marker_revision is not None
        and bundle_revision is not None
        and marker_revision != bundle_revision
    ):
        raise RuntimeError('installed source revision authorities disagree')
    if marker_revision is not None:
        return marker_revision
    if bundle_revision is not None:
        return bundle_revision

    try:
        revision = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            'source revision is unavailable outside an exact bundle or Git checkout'
        ) from error
    return _canonical_source_revision(revision, source='Git')


def web_bundle():
    assets = []
    types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'}
    for name in ('index.html', 'app.js', 'host-api-routes.js', 'styles.css'):
        body = (ROOT / 'web' / 'src' / name).read_bytes()
        assets.append(ImmutableWebAsset(name, body, sha256(body).hexdigest(), types[Path(name).suffix]))
    return ImmutableWebAssetBundle(source_revision(), HOST_API_CONTRACT_VERSION, tuple(assets))


class ProviderFreeApplication(EmbeddedWebHostApplication):
    def __init__(
        self,
        journal,
        *,
        state_dir,
        pairing_code,
        desktop_session_sink,
        **kwargs,
    ):
        self.state_dir = state_dir
        self._pairing_code = pairing_code
        self._pairing_lock = Lock()
        if not callable(desktop_session_sink):
            raise TypeError('desktop_session_sink must be callable')
        self._desktop_session_sink = desktop_session_sink
        # Bootstrap only one no-trade observation before command admission. The
        # empty-journal ownership rule remains intact and no order is sent.
        if not journal.load_events_by_aggregate_type('canonical_autonomous_simulation'):
            run_autonomous_simulation(PRICES, state_dir, run_id='provider-free-product',
                now=START_TIME, stop_after_episodes=1,
                execution_profile='TWO_EQUAL_PARTIALS',
                target_quantity='2')
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
            try:
                normalized = _headers(headers)
                if (normalized.get('origin') != self._origin
                    or normalized.get('content-type') != 'application/json'
                    or len(body) > 256 or self._pairing_code is None):
                    raise ValueError()
                # Pairing is the only unauthenticated API transition. Reuse the
                # canonical strict JSON ingress so duplicate keys and non-finite
                # constants cannot acquire owner-session authority.
                request = self._parse_body(body, normalized)
                if set(request) != {'pairing_code'} or type(request['pairing_code']) is not str:
                    raise ValueError()
                with self._pairing_lock:
                    if (
                        self._pairing_code is None
                        or not secrets.compare_digest(
                            request['pairing_code'],
                            self._pairing_code,
                        )
                    ):
                        raise ValueError()
                    session = self._boundary.create_session(
                        subject='local-owner',
                        role='OWNER',
                        origin=self._origin,
                        ttl_seconds=3600,
                    )
                    try:
                        self._desktop_session_sink(
                            origin=self._origin,
                            actor='local-owner',
                            token=session.token,
                        )
                    except Exception:
                        # Do not leave an unreported bearer alive when the
                        # protected current-user handoff to the native safety
                        # shell failed. Keep the one-time code available only
                        # because this claimant never received a usable session.
                        self._boundary.revoke_session(session.token)
                        return TransportResponse(
                            503,
                            'application/json',
                            b'{"error":"DESKTOP_SESSION_STORE_FAILED"}',
                            (('Cache-Control', 'no-store'),),
                        )
                    # Session issuance, native handoff and one-time code consume
                    # are one serialized authority transition.
                    self._pairing_code = None
                return TransportResponse(200, 'application/json', b'{"status":"PAIRED"}',
                    (('Set-Cookie', 'AutoTradeSession=' + session.token + '; HttpOnly; SameSite=Strict; Path=/api/v1; Max-Age=3600'),
                     ('Cache-Control', 'no-store')))
            except (ValueError, TypeError):
                return TransportResponse(403, 'application/json', b'{"error":"PAIRING_REJECTED"}')
        return super().dispatch(method=method, target=target, headers=headers, body=body)


def cookie_principal(headers, origin):
    del origin
    raw_cookie = headers.get('cookie', '')
    session_cookie_count = sum(
        1
        for part in raw_cookie.split(';')
        if part.strip().partition('=')[0].strip() == 'AutoTradeSession'
        and part.strip().partition('=')[1] == '='
    )
    if session_cookie_count > 1:
        raise PermissionError('Ambiguous AutoTrade session cookie')
    cookie = SimpleCookie()
    cookie.load(raw_cookie)
    if 'AutoTradeSession' not in cookie:
        return header_principal_resolver(headers, 'local')
    if session_cookie_count != 1:
        raise PermissionError('Malformed AutoTrade session cookie')
    token = cookie['AutoTradeSession'].value
    return HostPrincipal('local-owner', token, public_session_reference(token))


def _owned_desktop_session_sink(*, origin, actor, token):
    """Acknowledge direct parent pairing without persisting its short-lived bearer.

    The owned WPF parent is the pairing HTTP client and receives the exact token in
    that response. Persisting the same bearer in Credential Manager would widen
    the secret lifetime/surface without adding a recovery path: if the parent
    loses the response it terminates this child and a later launch pairs anew.
    """
    del origin, actor, token


def build_product(data_dir, *, port=0, desktop_session_sink=None):
    if port == 0:
        import socket
        # Reserve a candidate ephemeral port. The real listener still performs
        # exclusive bind before publishing its one-use pairing URL; a race fails
        # startup rather than pairing with another process.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
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
        try:
            inspected = inspect_canonical_simulation(state, history_limit=100)
        except SimulationStateChanging as error:
            raise SnapshotTemporarilyUnavailable(
                'canonical simulation state is changing'
            ) from error
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
            'strategy': {'strategy': 'moving-average-2-3-long-only-target-2',
                'decisions': [{k: v for k, v in e.items() if k != 'provider_state'} for e in completed[-100:]],
                'agent_decisions': [e['payload'] for e in store.load_events_by_aggregate_type('simulation_agent_decision')[-100:]],
                'economic_edge_status': 'INCONCLUSIVE'},
            'jobs': [{'kind': 'provider-free-research', 'result': e['payload']} for e in research] + [{'kind': 'provider-free-lifecycle', 'result': e['payload']} for e in receipts[-20:]],
            'reason_codes': ['SIMULATION_ONLY', 'ECONOMIC_EDGE_UNPROVEN']}

    config = ProductionHostConfig(state / 'journal.sqlite3', ACCOUNT, ENVIRONMENT,
        'local-simulation', '127.0.0.1', port, origin)
    session_sink = (
        persist_desktop_owner_session
        if desktop_session_sink is None
        else desktop_session_sink
    )
    if not callable(session_sink):
        raise TypeError('desktop_session_sink must be callable')
    runtime = build_production_host(config, security_boundary=boundary, principal_resolver=cookie_principal,
        snapshot_provider=snapshot, application_factory=partial(
            ProviderFreeApplication,
            state_dir=state,
            pairing_code=pairing,
            desktop_session_sink=session_sink,
        ))
    return runtime, origin + '/#pair=' + pairing


def restore_product_backup(backup_dir, data_dir):
    from tempfile import TemporaryDirectory
    from .backup import (
        BackupError,
        _fsync_directory,
        _fsync_directory_tree,
        restore_backup,
    )

    destination_root = Path(data_dir)
    if destination_root.exists():
        raise BackupError('Restore destination already exists')
    destination_root.parent.mkdir(parents=True, exist_ok=True)

    # The shared backup format restores artifacts alongside state, while ZERO
    # consumes state/artifacts. Convert that product-only layout in an
    # unpublished sibling tree. A crash/failure before final publication leaves
    # the requested destination absent and the exact restore retryable.
    with TemporaryDirectory(
        prefix='.autotrade-zero-restore-',
        dir=destination_root.parent,
    ) as scratch:
        root = restore_backup(backup_dir, Path(scratch) / 'product')
        source = root / 'artifacts'
        artifact_destination = root / 'state' / 'artifacts'
        if source.exists():
            if artifact_destination.exists():
                raise BackupError('restored artifact layout conflicts')
            source.rename(artifact_destination)
        _fsync_directory_tree(root)
        os.replace(root, destination_root)
        _fsync_directory(destination_root.parent)
    return destination_root


def _launch_message(launch_url, *, no_browser):
    if type(launch_url) is not str or not launch_url:
        raise ValueError('launch_url must be non-empty text')
    public_url, separator, pairing_code = launch_url.partition('#pair=')
    if separator != '#pair=' or not public_url or not pairing_code:
        raise ValueError('launch_url must contain one-time pairing material')
    if no_browser:
        return (
            'AutoTrade ZERO manual pairing URL (one-time local secret): '
            + launch_url
        )
    return (
        'AutoTrade ZERO started at '
        + public_url
        + '; opening the local browser for one-time pairing.'
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description='AutoTrade provider-free application')
    parser.add_argument('--data-dir', default=str(Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'AutoTrade-ZERO'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--desktop-child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--parent-pid', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--restore-backup', help='Verify and restore a backup into a NEW data directory; real trading stays gated')
    args = parser.parse_args(argv)
    if args.parent_pid is not None:
        from .product_worker import start_parent_watchdog
        start_parent_watchdog(args.parent_pid)
    if args.restore_backup:
        restore_product_backup(args.restore_backup, args.data_dir)
    runtime, launch_url = build_product(
        args.data_dir,
        port=args.port,
        desktop_session_sink=_owned_desktop_session_sink if args.desktop_child else None,
    )
    def stop(*_):
        Thread(target=runtime.close, daemon=True).start()
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, stop)
    if args.desktop_child:
        def desktop_control():
            for line in sys.stdin:
                if line.strip() == 'STOP':
                    stop()
                    return
            stop()
        Thread(target=desktop_control, daemon=True).start()
    if args.desktop_child:
        # Private owned stdout pipe; automatic user-facing launch never discloses the code.
        print('AutoTrade ZERO: ' + launch_url, flush=True)
    else:
        print(_launch_message(launch_url, no_browser=args.no_browser), flush=True)
    if not args.no_browser:
        if not webbrowser.open(launch_url):
            print(_launch_message(launch_url, no_browser=True), flush=True)
    try:
        runtime.serve_forever()
    finally:
        runtime.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
