import json
import os
from hashlib import sha256
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from tools import build_provider_free_candidate as candidate


def publish_evidence(source_sha, suite='provider-free-host-publish', *, result='PASS',
                     runner_os='Windows', workflow='provider-free-product'):
    return {
        'schema_version': '1.0.0',
        'source_sha': source_sha,
        'checked_out_sha': source_sha,
        'suite': suite,
        'command': 'test publish',
        'result': result,
        'runner_os': runner_os,
        'python_version': '3.12.10',
        'github': {
            'event_name': 'pull_request',
            'run_id': '1',
            'run_attempt': '1',
            'workflow': workflow,
        },
        'generated_at': '2026-10-05T10:00:00+00:00',
        'contains_secrets': False,
    }


def write_bound_host_publish(publish, source_sha, *, host_bytes=b'MZ-host',
                             evidence=None, runtime_bytes=None):
    (publish / 'AutoTrade.Host.exe').write_bytes(host_bytes)
    if runtime_bytes is not None:
        (publish / 'AutoTrade.Host.runtimeconfig.json').write_bytes(runtime_bytes)
    if evidence is None:
        evidence = publish_evidence(source_sha)
    (publish / 'host-build-evidence.json').write_text(
        json.dumps(evidence), encoding='utf-8')
    candidate.bind_publish_evidence(
        publish,
        executable='AutoTrade.Host.exe',
        evidence_name='host-build-evidence.json',
    )
    return json.loads(
        (publish / 'host-build-evidence.json').read_text(encoding='utf-8')
    )


class ProviderFreeCandidateInputAuthorityTests(unittest.TestCase):
    def test_relative_work_root_is_absolute_before_windows_namespace_staging(self):
        # The Windows provider-free job supplies --work artifacts/candidate-work.
        # A retained Windows namespace must not receive a relative parent.
        with TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            relative_work = root.relative_to(Path.cwd()) / 'candidate-work'
            with patch.object(candidate, 'stage_source', side_effect=RuntimeError('staging-probe')) as stage:
                with self.assertRaisesRegex(RuntimeError, 'staging-probe'):
                    candidate.build_candidate(
                        source_root=candidate.ROOT, source_sha='a' * 40,
                        desktop=root, host=root, python_archive=root / 'python.zip',
                        webview_archive=root / 'webview.nupkg', work=relative_work,
                        output=root / 'candidate.zip',
                    )
            self.assertEqual(stage.call_args.args[2], relative_work.absolute() / 'payload' / 'product')
            self.assertEqual(stage.call_args.args[3], relative_work.absolute() / 'source-composition.json')
            self.assertTrue(stage.call_args.args[3].is_absolute())

    def test_stage_source_uses_canonical_exact_git_reader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / 'source'; source_root.mkdir()
            destination = root / 'payload'
            composition = root / 'composition.json'
            source_sha = 'a' * 40
            paths = tuple(candidate.STATIC) + ('mvp/autotrade_mvp/product_runtime.py',)
            tree = ('\n'.join(paths) + '\n').encode('utf-8')
            with patch.object(candidate, '_git', return_value=tree) as exact_git, patch.object(
                candidate, '_stage_source_controlled_components') as stage:
                selected = candidate.stage_source(source_root, source_sha, destination, composition)
            exact_git.assert_called_once_with('ls-tree', '-r', '--name-only', source_sha, source_root=source_root)
            stage.assert_called_once()
            self.assertIn('mvp/autotrade_mvp/product_runtime.py', selected)
            self.assertIn('contracts/bindings/python/common_scalars.py', selected)
            self.assertEqual((destination / 'SOURCE_REVISION').read_text(encoding='utf-8'), source_sha + '\n')
            self.assertFalse(any(path.name.endswith('.lock') for path in destination.rglob('*')))

    def test_real_exact_source_staging_has_import_contracts_and_passes_content_gate(self):
        source_sha = subprocess.check_output(['git', '-C', str(candidate.ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = candidate.stage_source(candidate.ROOT, source_sha, root / 'product', root / 'composition.json')
            self.assertIn('contracts/bindings/python/common_scalars.py', selected)
            collected = {path for path, _absolute, _content in candidate._collect(root / 'product')}
            self.assertIn('mvp/autotrade_mvp/diagnostics.py', collected)
            self.assertIn('mvp/autotrade_mvp/decision_trace.py', collected)

    def test_archive_replacement_after_digest_check_cannot_change_extracted_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); archive = root / 'verified.zip'; replacement = root / 'replacement.zip'; destination = root / 'extracted'
            with zipfile.ZipFile(archive, 'w') as value: value.writestr('good.txt', b'verified bytes')
            with zipfile.ZipFile(replacement, 'w') as value: value.writestr('bad.txt', b'unverified replacement')
            verified_bytes = archive.read_bytes(); expected_digest = sha256(verified_bytes).hexdigest()
            original_read_bytes = Path.read_bytes; swapped = False
            def read_then_swap(path):
                nonlocal swapped
                payload = original_read_bytes(path)
                if path == archive and not swapped:
                    swapped = True; os.replace(replacement, archive)
                return payload
            with patch.object(Path, 'read_bytes', read_then_swap):
                candidate.extract_pinned(archive, destination, expected_digest)
            self.assertTrue(swapped)
            self.assertEqual((destination / 'good.txt').read_bytes(), b'verified bytes')
            self.assertFalse((destination / 'bad.txt').exists())
            self.assertFalse(any(path.name.endswith('.lock') for path in destination.rglob('*')))

    def test_archive_override_is_written_once_without_lock_sidecar(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); archive = root / 'python.zip'; destination = root / 'runtime'
            with zipfile.ZipFile(archive, 'w') as value:
                value.writestr('python312._pth', b'original path config'); value.writestr('python.exe', b'fake runtime bytes')
            candidate.extract_pinned(archive, destination, sha256(archive.read_bytes()).hexdigest(),
                overrides={'python312._pth': b'frozen isolated config\n'})
            self.assertEqual((destination / 'python312._pth').read_bytes(), b'frozen isolated config\n')
            self.assertEqual((destination / 'python.exe').read_bytes(), b'fake runtime bytes')
            self.assertFalse(any(path.name.endswith('.lock') for path in destination.rglob('*')))

    def test_transient_payload_writer_refuses_existing_leaf(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / 'payload.bin'
            candidate._write_new_payload_bytes(target, b'first')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                candidate._write_new_payload_bytes(target, b'second')
            self.assertEqual(target.read_bytes(), b'first')

    def test_host_publish_requires_exact_executable_and_exact_head_evidence(self):
        with TemporaryDirectory() as directory:
            publish = Path(directory); source_sha = 'a' * 40
            evidence = publish_evidence(source_sha)
            (publish / 'host-build-evidence.json').write_text(json.dumps(evidence), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Host publish is missing AutoTrade.Host.exe'):
                candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                    evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')
            host_bytes = b'MZ-fake-host-executable'
            (publish / 'AutoTrade.Host.exe').write_bytes(host_bytes)
            candidate.bind_publish_evidence(
                publish,
                executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json',
            )
            identity = candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')
            self.assertEqual(identity['sha256'], 'sha256:' + sha256(host_bytes).hexdigest())
            self.assertEqual(identity['bytes'], len(host_bytes))

    def test_bind_publish_evidence_covers_executable_and_runtime_payload(self):
        with TemporaryDirectory() as directory:
            publish = Path(directory); source_sha = 'a' * 40
            bound = write_bound_host_publish(
                publish,
                source_sha,
                host_bytes=b'MZ-bound-host',
                runtime_bytes=b'bound-runtime-config',
            )
            self.assertEqual(bound['artifact']['path'], 'AutoTrade.Host.exe')
            self.assertEqual(
                bound['artifact']['sha256'],
                'sha256:' + sha256(b'MZ-bound-host').hexdigest(),
            )
            self.assertEqual(bound['artifact']['bytes'], len(b'MZ-bound-host'))
            self.assertRegex(bound['publish_payload_sha256'], r'^sha256:[0-9a-f]{64}$')
            self.assertFalse(any(path.name.endswith('.lock') for path in publish.iterdir()))
            held = candidate._capture_publish(publish)
            self.assertEqual(
                bound['publish_payload_sha256'],
                candidate._publish_payload_digest(
                    held,
                    evidence_name='host-build-evidence.json',
                ),
            )

    def test_publish_evidence_rejects_same_sha_wrong_suite_or_workflow(self):
        with TemporaryDirectory() as directory:
            publish = Path(directory)
            source_sha = 'a' * 40
            cases = (
                publish_evidence(source_sha, suite='provider-free-desktop-publish'),
                publish_evidence(source_sha, workflow='Verify AutoTrade'),
                publish_evidence(source_sha, runner_os='Linux'),
            )
            for index, evidence in enumerate(cases):
                with self.subTest(index=index):
                    for child in tuple(publish.iterdir()):
                        child.unlink()
                    write_bound_host_publish(
                        publish,
                        source_sha,
                        evidence=evidence,
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        'selected exact-head build authority',
                    ):
                        candidate._require_publish_evidence(
                            publish,
                            executable='AutoTrade.Host.exe',
                            evidence_name='host-build-evidence.json',
                            source_sha=source_sha,
                            label='Host',
                        )

    def test_host_publish_rejects_stale_or_nonpassing_evidence(self):
        with TemporaryDirectory() as directory:
            publish = Path(directory); source_sha = 'a' * 40
            cases = (
                publish_evidence('b' * 40),
                publish_evidence(source_sha, result='FAIL'),
            )
            for index, evidence in enumerate(cases):
                with self.subTest(index=index):
                    for child in tuple(publish.iterdir()):
                        child.unlink()
                    write_bound_host_publish(
                        publish,
                        source_sha,
                        evidence=evidence,
                    )
                    with self.assertRaisesRegex(ValueError, 'Host publish evidence differs'):
                        candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                            evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')

    def test_host_executable_replacement_between_admission_and_copy_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); publish = root / 'publish'; publish.mkdir(); destination = root / 'payload'
            source_sha = 'a' * 40
            write_bound_host_publish(
                publish,
                source_sha,
                host_bytes=b'MZ-admitted-host',
            )
            admitted = candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')
            (publish / 'AutoTrade.Host.exe').write_bytes(b'MZ-replaced-host')
            snapshot = candidate._copy_publish(publish, destination)
            with self.assertRaisesRegex(ValueError, 'Host executable changed between admission and candidate copy'):
                candidate._require_copied_executable(admitted, snapshot, label='Host')
            self.assertEqual((destination / 'AutoTrade.Host.exe').read_bytes(), b'MZ-replaced-host')

    def test_publish_evidence_rejects_executable_replacement_before_held_snapshot(self):
        with TemporaryDirectory() as directory:
            publish = Path(directory); source_sha = 'a' * 40
            write_bound_host_publish(
                publish,
                source_sha,
                host_bytes=b'MZ-attested-host',
                runtime_bytes=b'attested-runtime',
            )
            (publish / 'AutoTrade.Host.exe').write_bytes(b'MZ-replaced-before-capture')
            held = candidate._capture_publish(publish)
            with self.assertRaisesRegex(ValueError, 'selected exact-head build authority'):
                candidate._require_publish_snapshot_evidence(
                    held,
                    executable='AutoTrade.Host.exe',
                    evidence_name='host-build-evidence.json',
                    source_sha=source_sha,
                    label='Host',
                )

    def test_publish_evidence_rejects_runtime_replacement_before_held_snapshot(self):
        with TemporaryDirectory() as directory:
            publish = Path(directory); source_sha = 'a' * 40
            write_bound_host_publish(
                publish,
                source_sha,
                host_bytes=b'MZ-attested-host',
                runtime_bytes=b'attested-runtime',
            )
            (publish / 'AutoTrade.Host.runtimeconfig.json').write_bytes(
                b'foreign-runtime-before-capture')
            held = candidate._capture_publish(publish)
            with self.assertRaisesRegex(ValueError, 'selected exact-head build authority'):
                candidate._require_publish_snapshot_evidence(
                    held,
                    executable='AutoTrade.Host.exe',
                    evidence_name='host-build-evidence.json',
                    source_sha=source_sha,
                    label='Host',
                )

    def test_held_host_publish_snapshot_prevents_post_admission_runtime_replacement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            publish = root / 'publish'
            publish.mkdir()
            destination = root / 'payload'
            source_sha = 'a' * 40
            host_bytes = b'MZ-admitted-host'
            runtime_bytes = b'admitted-runtime-dependency'
            evidence = write_bound_host_publish(
                publish,
                source_sha,
                host_bytes=host_bytes,
                runtime_bytes=runtime_bytes,
            )

            held = candidate._capture_publish(publish)
            identity = candidate._require_publish_snapshot_evidence(
                held,
                executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json',
                source_sha=source_sha,
                label='Host',
            )

            (publish / 'AutoTrade.Host.runtimeconfig.json').write_bytes(
                b'foreign-runtime-after-admission')
            (publish / 'host-build-evidence.json').write_text(
                json.dumps(publish_evidence(source_sha, result='FAIL')),
                encoding='utf-8',
            )

            copied = candidate._copy_publish_snapshot(held, destination)
            candidate._require_copied_executable(identity, copied, label='Host')
            self.assertEqual(
                (destination / 'AutoTrade.Host.runtimeconfig.json').read_bytes(),
                runtime_bytes,
            )
            self.assertEqual(
                json.loads((destination / 'host-build-evidence.json').read_text(encoding='utf-8')),
                evidence,
            )

    def test_publish_snapshot_evidence_rejects_malformed_held_bytes(self):
        source_sha = 'a' * 40
        snapshot = {
            'AutoTrade.Host.exe': b'MZ-host',
            'host-build-evidence.json': b'not-json',
        }
        with self.assertRaisesRegex(ValueError, 'not canonical JSON'):
            candidate._require_publish_snapshot_evidence(
                snapshot,
                executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json',
                source_sha=source_sha,
                label='Host',
            )

    def test_bind_publish_evidence_atomic_replace_does_not_write_through_raced_alias(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            publish = root / 'publish'
            publish.mkdir()
            source_sha = 'a' * 40
            evidence_path = publish / 'host-build-evidence.json'
            external = root / 'outside-authority.json'
            external_bytes = b'outside authority must remain untouched'
            external.write_bytes(external_bytes)
            (publish / 'AutoTrade.Host.exe').write_bytes(b'MZ-host')
            evidence_path.write_text(
                json.dumps(publish_evidence(source_sha)),
                encoding='utf-8',
            )

            original_replace = os.replace
            raced = []

            def race_with_hardlink(source, destination):
                if Path(destination) == evidence_path and not raced:
                    evidence_path.unlink()
                    os.link(external, evidence_path)
                    raced.append(True)
                return original_replace(source, destination)

            with patch.object(candidate.os, 'replace', side_effect=race_with_hardlink):
                candidate.bind_publish_evidence(
                    publish,
                    executable='AutoTrade.Host.exe',
                    evidence_name='host-build-evidence.json',
                )

            self.assertEqual(raced, [True])
            self.assertEqual(external.read_bytes(), external_bytes)
            self.assertFalse(os.path.samefile(evidence_path, external))
            self.assertFalse(
                any(path.name.endswith('.binding.tmp') for path in publish.iterdir())
            )
            candidate._require_publish_evidence(
                publish,
                executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json',
                source_sha=source_sha,
                label='Host',
            )


if __name__ == '__main__':
    unittest.main()
