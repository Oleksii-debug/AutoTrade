"""Composition adapter for the existing source-staging and Windows-bundle TCB.

This creates an unsigned diagnostics candidate, never a qualified release. Source
bytes come from one exact Git object; developer caches and state cannot enter it.
"""
import argparse
import base64
import binascii
from contextlib import ExitStack
import io
from hashlib import sha256, sha512
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile
import xml.etree.ElementTree as ET

from tools.stage_windows_foundation import (
    _SourceControlledComponent, _git, _stage_source_controlled_components,
    _retained_posix_directory, _retained_posix_relative_directory,
)
from tools.build_windows_bundle import build_bundle, _collect, _windows_path_key
from tools.release_scope_mapping import strict_json_bytes
from tools.dotnet_package_rights import _locked_nupkg_root_evidence
from research.autotrade_research.artifacts.durable_publish import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]
_PATH_TYPE = type(Path("."))
SOURCE_PREFIXES = ('mvp/autotrade_mvp/', 'research/autotrade_research/',
                   'autotrade_numeric/', 'autotrade_foundation/')
REVIEWED_LICENSE_PREFIX = 'provenance/licenses/'
STATIC = ('web/src/index.html', 'web/src/app.js', 'web/src/host-api-routes.js', 'web/src/styles.css',
          'contracts/openapi/host-api.yaml', 'contracts/bindings/python/common_scalars.py',
          'src/AutoTrade.Desktop/packages.lock.json',
          'packaging/windows/provider-free-inputs.json',
          'provenance/release-dependency-manifest.json',
          'provenance/dotnet-package-rights.json',
          'provenance/external-runtime-rights.json',
          'provenance/components.json',
          'provenance/reuse/autosport-neutral-primitives.json')


def _source_path_selected(path):
    if type(path) is not str or not path:
        raise TypeError('source path selector requires exact non-empty str')
    return (
        (path.startswith(SOURCE_PREFIXES) and path.endswith('.py'))
        or (
            path.startswith('contracts/jsonschema/')
            and path.endswith('.json')
        )
        or path.startswith(REVIEWED_LICENSE_PREFIX)
        or path in STATIC
    )


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
    if type(publish) is not _PATH_TYPE:
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
    if type(path) is not _PATH_TYPE or type(evidence) is not dict:
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
    selected = sorted(p for p in paths if _source_path_selected(p))
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
        source_root=source_root, descriptors=descriptors)
    _write_new_payload_bytes(destination / 'SOURCE_REVISION', (source_sha + '\n').encode())
    return selected


def extract_pinned(archive_path, destination, expected_digest, *, digest_algorithm='sha256', overrides=None):
    archive_bytes = archive_path.read_bytes()
    if digest_algorithm == 'sha256':
        actual_digest = sha256(archive_bytes).hexdigest()
    elif digest_algorithm == 'sha512-base64':
        actual_digest = base64.b64encode(sha512(archive_bytes).digest()).decode('ascii')
    else:
        raise ValueError('unsupported external archive digest algorithm')
    if actual_digest != expected_digest:
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


def _build_candidate_sbom(source_sha, inventory, inputs, webview_content_hash):
    if type(inventory) is not list:
        raise TypeError('candidate inventory must be a list')
    try:
        webview_sha512 = base64.b64decode(
            webview_content_hash,
            validate=True,
        ).hex()
    except (ValueError, binascii.Error) as error:
        raise ValueError('WebView2 content hash is invalid') from error
    python_sha256 = inputs['python']['sha256']
    python_version = inputs['python']['version']
    files = [
        {
            'SPDXID': 'SPDXRef-File-' + sha256(
                item['path'].encode('utf-8')
            ).hexdigest()[:32],
            'fileName': item['path'],
            'checksums': [{
                'algorithm': 'SHA256',
                'checksumValue': item['sha256'].removeprefix('sha256:'),
            }],
        }
        for item in inventory
    ]
    packages = [
        {
            'SPDXID': 'SPDXRef-Package-AutoTrade',
            'name': 'AutoTrade',
            'versionInfo': source_sha,
            'licenseConcluded': 'NOASSERTION',
            'primaryPackagePurpose': 'APPLICATION',
        },
        {
            'SPDXID': 'SPDXRef-Package-CPython',
            'name': 'CPython',
            'versionInfo': python_version,
            'licenseConcluded': 'PSF-2.0',
            'primaryPackagePurpose': 'RUNTIME',
            'externalRefs': [{
                'referenceCategory': 'PACKAGE_MANAGER',
                'referenceType': 'purl',
                'referenceLocator':
                    'pkg:generic/cpython-embed@' + python_version,
            }],
            'checksums': [{
                'algorithm': 'SHA256',
                'checksumValue': python_sha256,
            }],
        },
        {
            'SPDXID': 'SPDXRef-Package-WebView2',
            'name': 'Microsoft.Web.WebView2',
            'versionInfo': inputs['webview2_sdk']['version'],
            'licenseConcluded': 'BSD-3-Clause',
            'primaryPackagePurpose': 'LIBRARY',
            'externalRefs': [{
                'referenceCategory': 'PACKAGE_MANAGER',
                'referenceType': 'purl',
                'referenceLocator': (
                    'pkg:nuget/Microsoft.Web.WebView2@'
                    + inputs['webview2_sdk']['version']
                ),
            }],
            'checksums': [{
                'algorithm': 'SHA512',
                'checksumValue': webview_sha512,
            }],
        },
    ]
    return {
        'SPDXID': 'SPDXRef-DOCUMENT',
        'spdxVersion': 'SPDX-2.3',
        'dataLicense': 'CC0-1.0',
        'name': 'AutoTrade ZERO candidate SBOM',
        'documentNamespace': (
            'https://autotrade.invalid/spdx/' + source_sha
        ),
        'creationInfo': {
            'created': '1970-01-01T00:00:00Z',
            'creators': ['Tool: AutoTrade build_provider_free_candidate'],
        },
        'packages': packages,
        'files': files,
        'relationships': [
            {
                'spdxElementId': 'SPDXRef-DOCUMENT',
                'relationshipType': 'DESCRIBES',
                'relatedSpdxElement': 'SPDXRef-Package-AutoTrade',
            },
            {
                'spdxElementId': 'SPDXRef-Package-AutoTrade',
                'relationshipType': 'DEPENDS_ON',
                'relatedSpdxElement': 'SPDXRef-Package-CPython',
            },
            {
                'spdxElementId': 'SPDXRef-Package-AutoTrade',
                'relationshipType': 'DEPENDS_ON',
                'relatedSpdxElement': 'SPDXRef-Package-WebView2',
            },
        ],
    }


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


def _require_staged_reviewed_license_evidence(product_root):
    policy = strict_json_bytes(
        (product_root / 'provenance/dotnet-package-rights.json').read_bytes(),
        label='staged NuGet package rights',
    )
    records = policy.get('packages') if type(policy) is dict else None
    if (
        type(policy) is not dict
        or policy.get('schema_version') != '1.0.0'
        or type(records) is not list
    ):
        raise ValueError('staged NuGet package-rights schema is unsupported')
    seen = set()
    for record in records:
        if type(record) is not dict:
            raise ValueError('staged NuGet package-rights record is invalid')
        relative = record.get('expected_license_text_path')
        if (
            type(relative) is not str
            or not relative.startswith(REVIEWED_LICENSE_PREFIX)
            or '\\' in relative
            or PurePosixPath(relative).as_posix() != relative
            or any(part in {'', '.', '..'} for part in PurePosixPath(relative).parts)
            or not _source_path_selected(relative)
        ):
            raise ValueError('reviewed license evidence path is not stageable')
        if relative in seen:
            raise ValueError('reviewed license evidence path is duplicated')
        seen.add(relative)
        candidate = product_root.joinpath(*PurePosixPath(relative).parts)
        if not candidate.is_file() or not candidate.read_bytes():
            raise ValueError(
                'reviewed license evidence is absent from staged product: '
                + relative
            )
    return tuple(sorted(seen))


def _normalized_rights_text(payload, *, label):
    if type(payload) is not bytes:
        raise TypeError('rights text payload must be exact bytes')
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeDecodeError as error:
        raise ValueError(label + ' is not UTF-8') from error
    return (
        '\n'.join(
            line.rstrip()
            for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        ).strip()
        + '\n'
    )


def _require_webview2_archive_rights(
    product_root,
    archive_path,
    *,
    version,
    content_hash,
):
    if type(product_root) is not _PATH_TYPE or type(archive_path) is not _PATH_TYPE:
        raise TypeError('WebView2 rights verification requires exact Path values')
    if type(version) is not str or type(content_hash) is not str:
        raise TypeError('WebView2 rights identity must be exact str values')
    actual_hash = base64.b64encode(
        sha512(archive_path.read_bytes()).digest()
    ).decode('ascii')
    if actual_hash != content_hash:
        raise ValueError('WebView2 archive differs from locked rights identity')

    policy = strict_json_bytes(
        (product_root / 'provenance/dotnet-package-rights.json').read_bytes(),
        label='staged NuGet package rights',
    )
    records = policy.get('packages') if type(policy) is dict else None
    if (
        type(policy) is not dict
        or policy.get('schema_version') != '1.0.0'
        or type(records) is not list
    ):
        raise ValueError('staged NuGet package-rights schema is unsupported')
    matches = [
        record
        for record in records
        if (
            type(record) is dict
            and record.get('name') == 'Microsoft.Web.WebView2'
            and record.get('version') == version
            and record.get('content_hash_sha512_base64') == content_hash
        )
    ]
    if len(matches) != 1:
        raise ValueError('WebView2 archive lacks one exact staged rights record')
    record = matches[0]
    license_file = record.get('license_file')
    notice_file = record.get('notice_file')
    expected_path = record.get('expected_license_text_path')
    if (
        type(license_file) is not str
        or PurePosixPath(license_file).name != license_file
        or type(notice_file) is not str
        or PurePosixPath(notice_file).name != notice_file
        or type(expected_path) is not str
        or not expected_path.startswith(REVIEWED_LICENSE_PREFIX)
    ):
        raise ValueError('WebView2 staged rights record paths are invalid')

    license_bytes, notice_bytes, nuspec_bytes = _locked_nupkg_root_evidence(
        archive_path,
        license_file=license_file,
        notice_file=notice_file,
    )
    expected_license = product_root.joinpath(
        *PurePosixPath(expected_path).parts
    )
    if (
        not expected_license.is_file()
        or _normalized_rights_text(
            license_bytes,
            label='WebView2 package license',
        )
        != _normalized_rights_text(
            expected_license.read_bytes(),
            label='reviewed WebView2 license',
        )
    ):
        raise ValueError('WebView2 package license differs from reviewed evidence')
    if not notice_bytes:
        raise ValueError('WebView2 package notice is empty')

    try:
        root = ET.fromstring(nuspec_bytes)
    except ET.ParseError as error:
        raise ValueError('WebView2 package nuspec is invalid') from error
    metadata = [
        node for node in root.iter()
        if isinstance(node.tag, str)
        and node.tag.rsplit('}', 1)[-1] == 'metadata'
    ]
    if len(metadata) != 1:
        raise ValueError('WebView2 package nuspec metadata is ambiguous')
    children = {}
    for child in list(metadata[0]):
        if not isinstance(child.tag, str):
            continue
        local = child.tag.rsplit('}', 1)[-1]
        if local in children:
            raise ValueError('WebView2 package nuspec field is duplicated: ' + local)
        children[local] = child
    if (
        (children.get('id').text or '').strip()
        if children.get('id') is not None
        else None
    ) != 'Microsoft.Web.WebView2':
        raise ValueError('WebView2 package nuspec id mismatch')
    if (
        (children.get('version').text or '').strip()
        if children.get('version') is not None
        else None
    ) != version:
        raise ValueError('WebView2 package nuspec version mismatch')
    license_node = children.get('license')
    if (
        license_node is None
        or license_node.attrib != {'type': 'file'}
        or (license_node.text or '').strip() != license_file
    ):
        raise ValueError('WebView2 package nuspec license declaration mismatch')


def _require_webview2_input_identity(product_root, inputs, nuget_lock):
    if type(inputs) is not dict or type(inputs.get('webview2_sdk')) is not dict:
        raise ValueError('provider-free WebView2 input is missing')
    webview = inputs['webview2_sdk']
    if set(webview) != {'version', 'url', 'content_hash_sha512_base64'}:
        raise ValueError('provider-free WebView2 input fields mismatch')
    version = webview['version']
    content_hash = webview['content_hash_sha512_base64']
    url = webview['url']
    if type(version) is not str or not version or version != version.strip():
        raise ValueError('provider-free WebView2 version is invalid')
    if type(content_hash) is not str or not content_hash or content_hash != content_hash.strip():
        raise ValueError('provider-free WebView2 content hash is invalid')
    expected_url = (
        'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/'
        + version.lower()
        + '/microsoft.web.webview2.'
        + version.lower()
        + '.nupkg'
    )
    if url != expected_url:
        raise ValueError('provider-free WebView2 URL does not match exact version')
    if (
        type(nuget_lock) is not dict
        or set(nuget_lock) != {'version', 'dependencies'}
        or nuget_lock.get('version') != 1
        or type(nuget_lock.get('dependencies')) is not dict
    ):
        raise ValueError('provider-free NuGet lock schema is unsupported')
    lock_rows = []
    for target, target_dependencies in nuget_lock['dependencies'].items():
        if (
            type(target) is not str
            or not target
            or target != target.strip()
            or type(target_dependencies) is not dict
        ):
            raise ValueError('provider-free NuGet lock target is invalid')
        item = target_dependencies.get('Microsoft.Web.WebView2')
        if item is None:
            continue
        if (
            type(item) is not dict
            or set(item) != {'type', 'requested', 'resolved', 'contentHash'}
            or item.get('type') != 'Direct'
            or type(item.get('requested')) is not str
            or not item.get('requested')
            or item.get('requested') != item.get('requested').strip()
            or item.get('resolved') != version
            or item.get('contentHash') != content_hash
        ):
            raise ValueError(
                'provider-free WebView2 lock differs from frozen input'
            )
        lock_rows.append({
            'target': target,
            'type': item['type'],
            'requested': item['requested'],
            'resolved': item['resolved'],
            'content_hash_sha512_base64': item['contentHash'],
        })
    if len(lock_rows) != 1:
        raise ValueError(
            'provider-free WebView2 lock identity is not singular'
        )

    manifest_path = product_root / 'provenance/release-dependency-manifest.json'
    manifest = strict_json_bytes(
        manifest_path.read_bytes(),
        label='release dependency manifest',
    )
    dependencies = (
        manifest.get('dotnet_package_dependencies')
        if type(manifest) is dict
        else None
    )
    if type(dependencies) is not list:
        raise ValueError('release provenance has no .NET dependency graph')
    manifest_rows = [
        item
        for item in dependencies
        if type(item) is dict
        and item.get('name') == 'Microsoft.Web.WebView2'
    ]
    if len(manifest_rows) != 1:
        raise ValueError(
            'provider-free WebView2 release provenance is not singular'
        )
    lock_row = lock_rows[0]
    manifest_row = manifest_rows[0]
    expected_manifest = {
        'project': 'src/AutoTrade.Desktop/AutoTrade.Desktop.csproj',
        'target': lock_row['target'],
        'name': 'Microsoft.Web.WebView2',
        'type': lock_row['type'],
        'version': version,
        'content_hash_sha512_base64': content_hash,
        'dependencies': [],
        'requested': lock_row['requested'],
    }
    if manifest_row != expected_manifest:
        raise ValueError(
            'provider-free WebView2 lock differs from release provenance'
        )
    return content_hash


def build_candidate(*, source_root, source_sha, desktop, host, python_archive, webview_archive, work, output):
    if work.exists(): raise ValueError('candidate work directory must be new')
    work.mkdir(parents=True)
    payload = work / 'payload'; payload.mkdir()
    stage_source(source_root, source_sha, payload / 'product', work / 'source-composition.json')
    _require_staged_reviewed_license_evidence(payload / 'product')
    inputs = strict_json_bytes(
        (payload / 'product/packaging/windows/provider-free-inputs.json').read_bytes(),
        label='provider-free inputs',
    )
    nuget_lock = strict_json_bytes(
        (
            payload
            / 'product/src/AutoTrade.Desktop/packages.lock.json'
        ).read_bytes(),
        label='provider-free Desktop NuGet lock',
    )
    webview_content_hash = _require_webview2_input_identity(
        payload / 'product',
        inputs,
        nuget_lock,
    )
    _require_webview2_archive_rights(
        payload / 'product',
        webview_archive,
        version=inputs['webview2_sdk']['version'],
        content_hash=webview_content_hash,
    )

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
    extract_pinned(
        webview_archive,
        payload / 'notices/webview2-sdk-package',
        webview_content_hash,
        digest_algorithm='sha512-base64',
    )
    for required in ('python.exe', 'python312.dll', 'python312.zip', 'python312._pth', 'LICENSE.txt'):
        if not (payload / 'runtime/python' / required).is_file(): raise ValueError('embedded Python input is incomplete: ' + required)

    dependencies = {
        'source_sha': source_sha,
        'inputs': inputs,
        'nuget_lock': nuget_lock,
        'executables': {
            'desktop': {**desktop_identity, 'installed_path': 'AutoTrade.Desktop.exe'},
            'host': {**host_identity, 'installed_path': 'host/AutoTrade.Host.exe'},
        },
        'qualification': 'UNQUALIFIED', 'real_order_submission': 'UNAVAILABLE',
    }
    _write_new_payload_json(payload / 'dependency-lock.json', dependencies)
    inventory = [{'path': p, 'sha256': 'sha256:' + sha256(b).hexdigest()} for p, _, b in _collect(payload)]
    sbom = _build_candidate_sbom(
        source_sha,
        inventory,
        inputs,
        webview_content_hash,
    )
    _write_new_payload_json(payload / 'sbom.json', sbom)
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
    from tools.release_scope_mapping import build_mapping
    release_manifest = strict_json_bytes(
        (payload / 'product/provenance/release-dependency-manifest.json').read_bytes(),
        label='release dependency manifest',
    )
    dotnet_rights = strict_json_bytes(
        (payload / 'product/provenance/dotnet-package-rights.json').read_bytes(),
        label='dotnet package rights',
    )
    external_rights = strict_json_bytes(
        (payload / 'product/provenance/external-runtime-rights.json').read_bytes(),
        label='external runtime rights',
    )
    provenance_components = strict_json_bytes(
        (payload / 'product/provenance/components.json').read_bytes(),
        label='provenance components',
    )
    reuse_manifest = strict_json_bytes(
        (payload / 'product/provenance/reuse/autosport-neutral-primitives.json').read_bytes(),
        label='Autosport reuse provenance',
    )
    mapping = build_mapping(
        composition=composition,
        sbom=sbom,
        sbom_raw=(payload / 'sbom.json').read_bytes(),
        locked_packages=release_manifest['dotnet_package_dependencies'],
        package_rights=dotnet_rights['packages'],
        provenance_components=provenance_components['components'],
        reuse_documents=[reuse_manifest],
        external_runtime_rights=external_rights['runtimes'],
    )
    atomic_write_json(work / 'release-scope-mapping.json', mapping)
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
