import io
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.cli import get_status, main
from mvp.autotrade_mvp.pipeline import run_vertical_slice


class CliTests(unittest.TestCase):
    def test_status_before_first_run(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(get_status(directory), {"status": "not_started"})

    def test_status_after_run(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            status = get_status(directory)
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["symbol"], "SIM")
            self.assertEqual(status["evidence_count"], 1)
            self.assertEqual(len(status["postings"]), 1)
            self.assertEqual(len(status["fills"]), 1)

    def test_main_status_mode_is_executable(self):
        with TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch("sys.argv", ["autotrade", "--state-dir", directory, "--status"]):
                with patch("sys.stdout", output):
                    self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue()), {"status": "not_started"})


if __name__ == "__main__":
    unittest.main()
