"""Real child/pipe/lock lifecycle; workers are offline fixtures, never network."""
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_mvp.owned_numerical_research import OwnedNumericalResearch, OwnedResearchError


FIXTURE = '''
import asyncio
from pathlib import Path
import sys
from mvp.autotrade_mvp.owned_numerical_research import _serve_child
from research.autotrade_research.artifacts.resource_lock import ResourceLock
async def collector(*, market, state_dir, stop, install_signals):
    state_dir.mkdir(parents=True,exist_ok=True)
    with ResourceLock(state_dir/'whitebit-public-supervisor.lock'):
        await stop.wait()
        (state_dir/'checkpoint').write_text('STOPPED')
def learner(*, supervisor_state_dir, state_dir, interval_seconds, stop):
    state_dir.mkdir(parents=True,exist_ok=True)
    with ResourceLock(state_dir/'whitebit-live-zero-learning.lock'):
        memory=state_dir/'experience'
        count=int(memory.read_text()) if memory.exists() else 0
        stop.wait()
        memory.write_text(str(count+1))
try:
    asyncio.run(_serve_child(Path(sys.argv[1]),collector=collector,learner=learner))
except Exception:
    raise SystemExit(2)
'''


class OwnedNumericalResearchTests(unittest.TestCase):
    def setUp(self):
        real_popen = subprocess.Popen
        self.commands = []
        def offline(command, **kwargs):
            self.commands.append((command, kwargs))
            data_dir = command[command.index('--data-dir')+1]
            return real_popen([sys.executable, '-B', '-c', FIXTURE, data_dir], **kwargs)
        self.launch_patch = patch('autotrade_mvp.owned_numerical_research.subprocess.Popen', side_effect=offline)
        self.launch_patch.start()
        self.addCleanup(self.launch_patch.stop)

    def test_stop_restart_preserves_data_and_fixed_no_window_command(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            owner=OwnedNumericalResearch(root)
            for count in (1,2):
                owner.start()
                result=owner.stop()
                self.assertTrue(result['checkpoint_confirmed'])
                self.assertEqual((root/'whitebit-live-zero-learning'/'experience').read_text(),str(count))
                self.assertEqual((root/'whitebit-public-supervisor'/'checkpoint').read_text(),'STOPPED')
            command,kwargs=self.commands[0]
            self.assertEqual(command[3],'mvp.autotrade_mvp.owned_numerical_research')
            self.assertEqual(kwargs['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertFalse(kwargs.get('shell',False))

    def test_duplicate_owner_cannot_stop_or_replace_first_child(self):
        with TemporaryDirectory() as tmp:
            first=OwnedNumericalResearch(Path(tmp))
            second=OwnedNumericalResearch(Path(tmp))
            first.start()
            try:
                with self.assertRaisesRegex(OwnedResearchError,'ownership'):
                    second.start()
                second.terminate_after_failed_stop()
                self.assertIsNone(first._process.poll())
            finally:
                first.stop()

    def test_parent_pipe_eof_gracefully_checkpoints(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            owner=OwnedNumericalResearch(root)
            owner.start()
            owner._process.stdin.close()
            self.assertEqual(owner._process.wait(timeout=10),0)
            self.assertTrue(owner.stop()['checkpoint_confirmed'])
            self.assertEqual((root/'whitebit-live-zero-learning'/'experience').read_text(),'1')

    def test_stop_failure_is_reported_before_explicit_forced_termination(self):
        with TemporaryDirectory() as tmp:
            owner=OwnedNumericalResearch(Path(tmp))
            owner.start()
            child=owner._process
            with self.assertRaisesRegex(OwnedResearchError,'requires'):
                owner.terminate_after_failed_stop()
            with patch.object(child,'wait',side_effect=subprocess.TimeoutExpired('fixture',0)):
                with self.assertRaisesRegex(OwnedResearchError,'checkpoint unconfirmed'):
                    owner.stop(timeout=0)
            self.assertFalse(owner.terminate_after_failed_stop()['checkpoint_confirmed'])

    def test_external_worker_lock_is_not_stolen(self):
        from research.autotrade_research.artifacts.resource_lock import ResourceLock
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            collector=root/'whitebit-public-supervisor'
            collector.mkdir()
            with ResourceLock(collector/'whitebit-public-supervisor.lock'):
                owner=OwnedNumericalResearch(root)
                with self.assertRaisesRegex(OwnedResearchError,'ownership'):
                    owner.start()
                owner.terminate_after_failed_stop()
                self.assertFalse((collector/'checkpoint').exists())

    def test_updated_product_directory_cannot_be_data_directory(self):
        product=Path(__file__).resolve().parents[2]
        with self.assertRaisesRegex(OwnedResearchError,'outside'):
            OwnedNumericalResearch(product/'transient-data')


if __name__ == '__main__':
    unittest.main()
