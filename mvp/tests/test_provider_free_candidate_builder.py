import os
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from tools import build_provider_free_candidate as candidate


class ProviderFreeCandidateInputAuthorityTests(unittest.TestCase):
    def test_stage_source_uses_canonical_exact_git_reader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / 'source'
            source_root.mkdir()
            destination = root / 'payload'
            composition = root / 'composition.json'
            source_sha = 'a' * 40
            paths = tuple(candidate.STATIC) + (
                'mvp/autotrade_mvp/product_runtime.py',
                'contracts/bindings/python/common_scalars.py',
            )
            tree = ('\n'.join(paths) + '\n').encode('utf-8')

            with patch.object(candidate, '_git', return_value=tree) as exact_git, patch.object(
                candidate,
                '_stage_source_controlled_components',
            ) as stage:
                selected = candidate.stage_source(
                    source_root,
                    source_sha,
                    destination,
                    composition,
                )

            exact_git.assert_called_once_with(
                'ls-tree',
                '-r',
                '--name-only',
                source_sha,
                source_root=source_root,
            )
            stage.assert_called_once()
            self.assertIn('mvp/autotrade_mvp/product_runtime.py', selected)
            self.assertIn('contracts/bindings/python/common_scalars.py', selected)
            self.assertEqual(
                (destination / 'SOURCE_REVISION').read_text(encoding='utf-8'),
                source_sha + '\n',
            )

    def test_real_exact_source_staging_has_import_contracts_and_passes_content_gate(self):
        import subprocess
        source_sha = subprocess.check_output(
            ['git', '-C', str(candidate.ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected = candidate.stage_source(candidate.ROOT, source_sha,
                root / 'product', root / 'composition.json')
            self.assertIn('contracts/bindings/python/common_scalars.py', selected)
            # Exercise actual directory authority and secret-content gates, not
            # mocked staging. The committed source must be safely packageable.
            files = candidate._collect(root / 'product')
            self.assertIn('mvp/autotrade_mvp/diagnostics.py', {p for p, _, _ in files})

    def test_archive_replacement_after_digest_check_cannot_change_extracted_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'verified.zip'
            replacement = root / 'replacement.zip'
            destination = root / 'extracted'

            with zipfile.ZipFile(archive, 'w') as value:
                value.writestr('good.txt', b'verified bytes')
            with zipfile.ZipFile(replacement, 'w') as value:
                value.writestr('bad.txt', b'unverified replacement')

            verified_bytes = archive.read_bytes()
            expected_digest = sha256(verified_bytes).hexdigest()
            original_read_bytes = Path.read_bytes
            swapped = False

            def read_then_swap(path):
                nonlocal swapped
                payload = original_read_bytes(path)
                if path == archive and not swapped:
                    swapped = True
                    os.replace(replacement, archive)
                return payload

            with patch.object(Path, 'read_bytes', read_then_swap):
                candidate.extract_pinned(
                    archive,
                    destination,
                    expected_digest,
                )

            self.assertTrue(swapped)
            self.assertEqual(
                (destination / 'good.txt').read_bytes(),
                b'verified bytes',
            )
            self.assertFalse((destination / 'bad.txt').exists())


if __name__ == '__main__':
    unittest.main()
