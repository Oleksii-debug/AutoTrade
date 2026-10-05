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


def _capture_publish(publish):
    if type(publish) is not Path:
        raise TypeError('publish root must be an exact Path')
    captured = {}
    for relative, _, content in _collect(publish):
        if relative in captured:
            raise ValueError('publish snapshot contains a duplicate path: ' + relative)
        captured[relative] = content
    return captured


def _publish_payload_digest(snapshot, *, evidence_name):
    if (type(snapshot) is not dict or type(evidence_name) is not str
        or not evidence_name or evidence_name != evidence_name.strip()
        or any(type(path) is not str or type(content) is not bytes
               for path, content in snapshot.items())):
        raise TypeError('publish payload digest requires an exact snapshot and evidence name')
    if evidence_name not in snapshot:
        raise ValueError('publish evidence is absent from snapshot')
    manifest = [
        {
            'path': path,
            'sha256': 'sha256:' + sha256(snapshot[path]).hexdigest(),
            'bytes': len(snapshot[path]),
        }
        for path in sorted(snapshot)
        if path != evidence_name
    ]
    if not manifest:
        raise ValueError('publish snapshot has no payload outside evidence')
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('utf-8')
    return 'sha256:' + sha256(encoded).hexdigest()


def _rewrite_existing_publish_evidence(path, evidence):
    """Atomically replace one existing CI evidence leaf without lock metadata.

    Never reopen the admitted pathname for destructive in-place writing. A
    namespace race after the no-follow leaf check can therefore replace only a
    directory entry; it cannot redirect binder bytes through a substituted
    symlink or hardlink into another authority-owned file.
    """
    if type(path) is not Path or type(evidence) is not dict:
        raise TypeError('publish evidence rewrite requires exact Path and dict')
    payload = (
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    ).encode('utf-8')
    try:
        info = path.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError) as error:
        raise ValueError('publish evidence leaf is unavailable for binding') from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('publish evidence leaf must be one ordinary unaliased file')

    temporary = path.with_name(
        '.' + path.name + '.' + sha256(payload).hexdigest()[:16] + '.binding.tmp'
    )
    try:
        _write_new_payload_bytes(temporary, payload)
        try:
            # Namespace replacement is atomic and never opens the raced target
            # for writing, so a substituted alias cannot redirect the bytes.
            os.replace(temporary, path)
        except OSError as error:
            raise ValueError(
                'publish evidence leaf could not be atomically replaced'
            ) from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ValueError(
                'publish evidence temporary leaf could not be removed'
            ) from error


def bind_publish_evidence(publish, *, executable, evidence_name):
    """Bind CI PASS evidence to the exact publish payload it is meant to attest.

    The evidence file itself is excluded from the payload digest to avoid a
    self-referential hash. Every other held publish byte, including the primary
    executable and runtime/config dependencies, is covered.
    """
    snapshot = _capture_publish(publish)
    if type(executable) is not str or not executable or executable != executable.strip():
        raise TypeError('publish executable must be canonical text')
    executable_bytes = snapshot.get(executable)
    if executable_bytes is None:
        raise ValueError('publish binding is missing ' + executable)
    evidence_bytes = snapshot.get(evidence_name)
    if evidence_bytes is None:
        raise ValueError('publish binding evidence is missing')
    try:
        evidence = json.loads(evidence_bytes.decode('utf-8', errors='strict'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('publish binding evidence is not JSON') from error
    if type(evidence) is not dict:
        raise ValueError('publish binding evidence must be an object')
    if 'artifact' in evidence or 'publish_payload_sha256' in evidence:
        raise ValueError('publish evidence is already payload-bound')
    evidence['artifact'] = {
        'path': executable,
        'sha256': 'sha256:' + sha256(executable_bytes).hexdigest(),
        'bytes': len(executable_bytes),
    }
    evidence['publish_payload_sha256'] = _publish_payload_digest(
        snapshot,
        evidence_name=evidence_name,
    )
    _rewrite_existing_publish_evidence(publish / evidence_name, evidence)
    return evidence


def _require_publish_snapshot_evidence(snapshot, *, executable, evidence_name, source_sha, label):
    if (type(snapshot) is not dict or any(type(path) is not str or type(content) is not bytes
                                          for path, content in snapshot.items())
        or type(executable) is not str or type(evidence_name) is not str or type(label) is not str):
        raise TypeError('publish evidence inputs must use exact canonical types')
    evidence_contract = {
        'Desktop': 'provider-free-desktop-publish',
        'Host': 'provider-free-host-publish',
    }.get(label)
    if evidence_contract is None:
        raise ValueError('publish evidence label has no selected authority contract: ' + label)
    executable_bytes = snapshot.get(executable)
    if executable_bytes is None:
        raise ValueError(label + ' publish is missing ' + executable)
    evidence_bytes = snapshot.get(evidence_name)
    if evidence_bytes is None:
        raise ValueError(label + ' publish evidence is missing')
    try:
        evidence = json.loads(evidence_bytes.decode('utf-8', errors='strict'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(label + ' publish evidence is not canonical JSON') from error
    github = evidence.get('github') if type(evidence) is dict else None
    expected_artifact = {
        'path': executable,
        'sha256': 'sha256:' + sha256(executable_bytes).hexdigest(),
        'bytes': len(executable_bytes),
    }
    artifact = evidence.get('artifact') if type(evidence) is dict else None
    expected_payload_digest = _publish_payload_digest(
        snapshot,
        evidence_name=evidence_name,
    )
    if (type(evidence) is not dict
        or evidence.get('schema_version') != '1.0.0'
        or evidence.get('source_sha') != source_sha
        or evidence.get('checked_out_sha') != source_sha
        or evidence.get('suite') != evidence_contract
        or evidence.get('result') != 'PASS'
        or evidence.get('runner_os') != 'Windows'
        or evidence.get('python_version') != '3.12.10'
        or evidence.get('contains_secrets') is not False
        or type(github) is not dict
        or github.get('workflow') != 'provider-free-product'
        or type(artifact) is not dict
        or set(artifact) != {'path', 'sha256', 'bytes'}
        or type(artifact.get('path')) is not str
        or type(artifact.get('sha256')) is not str
        or type(artifact.get('bytes')) is not int
        or artifact != expected_artifact
        or type(evidence.get('publish_payload_sha256')) is not str
        or evidence.get('publish_payload_sha256') != expected_payload_digest):
        raise ValueError(label + ' publish evidence differs from the selected exact-head build authority')
    return expected_artifact


def _require_publish_evidence(publish, *, executable, evidence_name, source_sha, label):
    return _require_publish_snapshot_evidence(
        _capture_publish(publish),
        executable=executable,
        evidence_name=evidence_name,
        source_sha=source_sha,
        label=label,
    )


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


def _copy_publish_snapshot(publish_snapshot, destination):
    if (type(publish_snapshot) is not dict
        or any(type(path) is not str or type(content) is not bytes
               for path, content in publish_snapshot.items())):
        raise TypeError('publish snapshot must be an exact str-to-bytes dict')
    copied = {}
    for relative in sorted(publish_snapshot):
        content = publish_snapshot[relative]
        identity = {'sha256': 'sha256:' + sha256(content).hexdigest(), 'bytes': len(content)}
        copied[relative] = identity
        _write_new_payload_bytes(destination / relative, content)
    return copied


def _copy_publish(publish, destination):
    return _copy_publish_snapshot(_capture_publish(publish), destination)


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

    desktop_publish_snapshot = _capture_publish(desktop)
    host_publish_snapshot = _capture_publish(host)
    desktop_identity = _require_publish_snapshot_evidence(
        desktop_publish_snapshot, executable='AutoTrade.Desktop.exe',
        evidence_name='desktop-build-evidence.json', source_sha=source_sha, label='Desktop')
    host_identity = _require_publish_snapshot_evidence(
        host_publish_snapshot, executable='AutoTrade.Host.exe',
        evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')

    desktop_snapshot = _copy_publish_snapshot(desktop_publish_snapshot, payload)
    host_snapshot = _copy_publish_snapshot(host_publish_snapshot, payload / 'host')
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
