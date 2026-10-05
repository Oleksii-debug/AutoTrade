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


class ProviderFreeCandidateInputAuthorityTests(unittest.TestCase):
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
            evidence = {'source_sha': source_sha, 'checked_out_sha': source_sha, 'result': 'PASS'}
            (publish / 'host-build-evidence.json').write_text(json.dumps(evidence), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Host publish is missing AutoTrade.Host.exe'):
                candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                    evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')
            host_bytes = b'MZ-fake-host-executable'; (publish / 'AutoTrade.Host.exe').write_bytes(host_bytes)
            identity = candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')
            self.assertEqual(identity['sha256'], 'sha256:' + sha256(host_bytes).hexdigest())
            self.assertEqual(identity['bytes'], len(host_bytes))

    def test_host_publish_rejects_stale_or_nonpassing_evidence(self):
        with TemporaryDirectory() as directory:
            publish = Path(directory); (publish / 'AutoTrade.Host.exe').write_bytes(b'MZ-host'); source_sha = 'a' * 40
            for evidence in (
                {'source_sha': 'b' * 40, 'checked_out_sha': 'b' * 40, 'result': 'PASS'},
                {'source_sha': source_sha, 'checked_out_sha': source_sha, 'result': 'FAIL'},
            ):
                (publish / 'host-build-evidence.json').write_text(json.dumps(evidence), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'Host publish evidence differs'):
                    candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                        evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')

    def test_host_executable_replacement_between_admission_and_copy_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); publish = root / 'publish'; publish.mkdir(); destination = root / 'payload'
            source_sha = 'a' * 40
            (publish / 'AutoTrade.Host.exe').write_bytes(b'MZ-admitted-host')
            (publish / 'host-build-evidence.json').write_text(json.dumps({
                'source_sha': source_sha, 'checked_out_sha': source_sha, 'result': 'PASS'}), encoding='utf-8')
            admitted = candidate._require_publish_evidence(publish, executable='AutoTrade.Host.exe',
                evidence_name='host-build-evidence.json', source_sha=source_sha, label='Host')
            (publish / 'AutoTrade.Host.exe').write_bytes(b'MZ-replaced-host')
            snapshot = candidate._copy_publish(publish, destination)
            with self.assertRaisesRegex(ValueError, 'Host executable changed between admission and candidate copy'):
                candidate._require_copied_executable(admitted, snapshot, label='Host')
            self.assertEqual((destination / 'AutoTrade.Host.exe').read_bytes(), b'MZ-replaced-host')


if __name__ == '__main__':
    unittest.main()
