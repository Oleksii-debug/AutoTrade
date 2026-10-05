"""Composition adapter for the existing source-staging and Windows-bundle TCB.

This creates an unsigned diagnostics candidate, never a qualified release. Source
bytes come from one exact Git object; developer caches and state cannot enter it.
"""
import argparse
from contextlib import ExitStack
import io
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

from tools.stage_windows_foundation import (
    _SourceControlledComponent, _git, _stage_source_controlled_components,
    _retained_posix_directory, _retained_posix_relative_directory,
)
from tools.build_windows_bundle import build_bundle, _collect, _windows_path_key
from research.autotrade_research.artifacts.durable_publish import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PREFIXES = ('mvp/autotrade_mvp/', 'research/autotrade_research/',
                   'autotrade_numeric/', 'autotrade_foundation/')
STATIC = ('web/src/index.html', 'web/src/app.js', 'web/src/host-api-routes.js', 'web/src/styles.css',
          'contracts/openapi/host-api.yaml', 'contracts/bindings/python/common_scalars.py',
          'src/AutoTrade.Desktop/packages.lock.json',
          'packaging/windows/provider-free-inputs.json', 'provenance/release-dependency-manifest.json')


def _write_new_payload_bytes(path, payload):
    if type(payload) is not bytes:
        raise TypeError('payload bytes must be exact bytes')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise ValueError('candidate payload leaf already exists: ' + str(path)) from error


def _write_new_payload_json(path, value):
    if type(value) is not dict:
        raise TypeError('candidate payload JSON must be an exact dict')
    payload = (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode('utf-8')
    _write_new_payload_bytes(path, payload)


def _require_publish_evidence(publish, *, executable, evidence_name, source_sha, label):
    if type(publish) is not Path or type(executable) is not str or type(evidence_name) is not str or type(label) is not str:
        raise TypeError('publish evidence inputs must use exact canonical types')
    executable_path = publish / executable
    if not executable_path.is_file():
        raise ValueError(label + ' publish is missing ' + executable)
    evidence_path = publish / evidence_name
    if not evidence_path.is_file():
        raise ValueError(label + ' publish evidence is missing')
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    if (type(evidence) is not dict or evidence.get('source_sha') != source_sha
        or evidence.get('checked_out_sha') != source_sha or evidence.get('result') != 'PASS'):
        raise ValueError(label + ' publish evidence differs from the exact product source')
    executable_bytes = executable_path.read_bytes()
    return {'path': executable, 'sha256': 'sha256:' + sha256(executable_bytes).hexdigest(), 'bytes': len(executable_bytes)}


def stage_source(source_root, source_sha, destination, composition_path):
    if len(source_sha) != 40 or any(c not in '0123456789abcdef' for c in source_sha):
        raise ValueError('exact lowercase Git source SHA required')
    try:
        paths = _git('ls-tree', '-r', '--name-only', source_sha, source_root=source_root).decode('utf-8', errors='strict').splitlines()
    except UnicodeDecodeError as error:
        raise ValueError('exact Git tree contains a non-UTF-8 product path') from error
    selected = sorted(p for p in paths if (p.startswith(SOURCE_PREFIXES) and p.endswith('.py'))
        or (p.startswith('contracts/jsonschema/') and p.endswith('.json')) or p in STATIC)
    if not set(STATIC).issubset(selected) or 'mvp/autotrade_mvp/product_runtime.py' not in selected:
        raise ValueError('committed product composition is incomplete')
    destination.mkdir(parents=True, exist_ok=False)
    atomic_write_json(composition_path, {'source_sha': source_sha, 'components': []})
    descriptors = tuple(_SourceControlledComponent('zero-' + sha256(p.encode()).hexdigest()[:32], 'runtime-source', p) for p in selected)
    if os.name != 'nt':
        with _retained_posix_directory(destination, label='candidate source root') as root_descriptor:
            for parts in sorted({PurePosixPath(p).parent.parts for p in selected}):
                with ExitStack() as parents:
                    descriptor = root_descriptor
                    for part in parts:
                        descriptor = parents.enter_context(_retained_posix_relative_directory(descriptor, (part,), create=True))
    _stage_source_controlled_components(staging=destination, composition_path=composition_path,
        source_root=source_root, descriptors=descriptors, expected_source_sha=source_sha)
    _write_new_payload_bytes(destination / 'SOURCE_REVISION', (source_sha + '\n').encode())
    return selected


def extract_pinned(archive_path, destination, expected_digest, *, overrides=None):
    archive_bytes = archive_path.read_bytes()
    if sha256(archive_bytes).hexdigest() != expected_digest:
        raise ValueError('external runtime archive differs from frozen input')
    destination.mkdir(parents=True, exist_ok=False)
    if overrides is None: overrides = {}
    if (type(overrides) is not dict or any(type(key) is not str or type(value) is not bytes for key, value in overrides.items())):
        raise TypeError('archive overrides must be an exact str-to-bytes dict')
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        seen_windows_keys = set(); seen_paths = set()
        for entry in archive.infolist():
            relative = PurePosixPath(entry.filename)
            if (relative.is_absolute() or '..' in relative.parts or '\\' in entry.filename or ':' in entry.filename
                or relative.as_posix() != entry.filename.rstrip('/') or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('external archive contains unsafe paths')
            key = _windows_path_key(relative.as_posix())
            if key in seen_windows_keys: raise ValueError('external archive contains colliding paths')
            seen_windows_keys.add(key); exact_path = relative.as_posix(); seen_paths.add(exact_path)
            target = destination.joinpath(*relative.parts)
            if entry.is_dir(): target.mkdir(parents=True, exist_ok=True)
            else:
                content = overrides.get(entry.filename)
                if content is None: content = archive.read(entry)
                _write_new_payload_bytes(target, content)
        missing_overrides = set(overrides) - seen_paths
        if missing_overrides: raise ValueError('archive override path is absent: ' + ','.join(sorted(missing_overrides)))


def _copy_publish(publish, destination):
    snapshot = {}
    for relative, _, content in _collect(publish):
        identity = {'sha256': 'sha256:' + sha256(content).hexdigest(), 'bytes': len(content)}
        snapshot[relative] = identity
        _write_new_payload_bytes(destination / relative, content)
    return snapshot


def _require_copied_executable(expected, snapshot, *, label):
    actual = snapshot.get(expected['path'])
    if actual is None or actual.get('sha256') != expected['sha256'] or actual.get('bytes') != expected['bytes']:
        raise ValueError(label + ' executable changed between admission and candidate copy')


def build_candidate(*, source_root, source_sha, desktop, host, python_archive, webview_archive, work, output):
    if work.exists(): raise ValueError('candidate work directory must be new')
    work.mkdir(parents=True)
    payload = work / 'payload'; payload.mkdir()
    stage_source(source_root, source_sha, payload / 'product', work / 'source-composition.json')
    inputs = json.loads((payload / 'product/packaging/windows/provider-free-inputs.json').read_text())

    desktop_identity = _require_publish_evidence(desktop, executable='AutoTrade.Desktop.exe',
        evidence_name='desktop-build-evidence.json', source_sha=source_sha, label='Desktop')
    host_identity = _require_publish_evidence(host, executable='AutoTrade.Host.exe',
        evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')

    desktop_snapshot = _copy_publish(desktop, payload)
    host_snapshot = _copy_publish(host, payload / 'host')
    _require_copied_executable(desktop_identity, desktop_snapshot, label='Desktop')
    _require_copied_executable(host_identity, host_snapshot, label='Host')

    isolated_python_path = b'python312.zip\n.\n../../product\n../../product/research\n'
    extract_pinned(python_archive, payload / 'runtime/python', inputs['python']['sha256'], overrides={'python312._pth': isolated_python_path})
    extract_pinned(webview_archive, payload / 'notices/webview2-sdk-package', inputs['webview2_sdk']['sha256'])
    for required in ('python.exe', 'python312.dll', 'python312.zip', 'python312._pth', 'LICENSE.txt'):
        if not (payload / 'runtime/python' / required).is_file(): raise ValueError('embedded Python input is incomplete: ' + required)

    dependencies = {
        'source_sha': source_sha,
        'inputs': inputs,
        'nuget_lock': json.loads((payload / 'product/src/AutoTrade.Desktop/packages.lock.json').read_text()),
        'executables': {
            'desktop': {**desktop_identity, 'installed_path': 'AutoTrade.Desktop.exe'},
            'host': {**host_identity, 'installed_path': 'host/AutoTrade.Host.exe'},
        },
        'qualification': 'UNQUALIFIED', 'real_order_submission': 'UNAVAILABLE',
    }
    _write_new_payload_json(payload / 'dependency-lock.json', dependencies)
    inventory = [{'path': p, 'sha256': 'sha256:' + sha256(b).hexdigest()} for p, _, b in _collect(payload)]
    _write_new_payload_json(payload / 'sbom.json', {'source_sha': source_sha, 'files': inventory,
        'rights_review': 'PENDING', 'advisory_review': 'PENDING'})
    files = _collect(payload)
    components = [{'component_id': 'candidate-' + sha256(p.encode()).hexdigest()[:32],
        'kind': 'dependency-lock' if p == 'dependency-lock.json' else 'sbom' if p == 'sbom.json' else 'product-file',
        'path': p, 'version': source_sha, 'sha256': 'sha256:' + sha256(b).hexdigest()} for p, _, b in files]
    by_path = {c['path']: c['sha256'] for c in components}
    from mvp.autotrade_mvp.persistence import JournalStore
    composition = {'schema_version': '1.0.0', 'product': 'AutoTrade', 'source_sha': source_sha,
        'dependency_lock_sha256': by_path['dependency-lock.json'], 'sbom_sha256': by_path['sbom.json'],
        'schema_compatibility': {'minimum': str(JournalStore.SCHEMA_VERSION), 'maximum': str(JournalStore.SCHEMA_VERSION)},
        'runtime': {'architecture': 'x64', 'runtime_identifier': 'win-x64', 'minimum_windows_version': 'Windows 11'},
        'components': components}
    composition_path = work / 'windows-composition.json'; atomic_write_json(composition_path, composition)
    result = build_bundle(staging=payload, output=output, version='0.1.0-zero-candidate', source_sha=source_sha,
        mode='diagnostics', provenance_path=payload / 'product/provenance/release-dependency-manifest.json',
        composition_path=composition_path)
    atomic_write_json(work / 'candidate-result.json', {'source_sha': source_sha, 'package_sha256': result['sha256'],
        'file_count': len(files), 'desktop_executable': dependencies['executables']['desktop'],
        'host_executable': dependencies['executables']['host'], 'release_eligible': False, 'nvda_verified': False})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--desktop-publish', type=Path, required=True)
    parser.add_argument('--host-publish', type=Path, required=True)
    parser.add_argument('--python-archive', type=Path, required=True)
    parser.add_argument('--webview-archive', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build_candidate(source_root=ROOT, source_sha=args.source_sha, desktop=args.desktop_publish,
        host=args.host_publish, python_archive=args.python_archive, webview_archive=args.webview_archive,
        work=args.work, output=args.output)
    print(json.dumps({'source_sha': args.source_sha, 'package_sha256': result['sha256'], 'release_eligible': False}))


if __name__ == '__main__':
    main()
