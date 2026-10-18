from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.whitebit_credentials import (
    WHITEBIT_PRIMARY_PROFILE,
    WhitebitReadCredentials,
    _target,
)


class WhitebitCredentialTests(unittest.TestCase):
    def test_exact_read_only_profile_and_non_secret_reference(self):
        self.assertEqual(WHITEBIT_PRIMARY_PROFILE, "whitebit-primary")
        self.assertEqual(
            _target(), "AutoTrade/ProviderRead/v1/whitebit/whitebit-primary"
        )
        for value in ("owner", "../whitebit-primary", "WHITEBIT-PRIMARY", 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _target(value)

    def test_credential_repr_never_contains_key_or_secret(self):
        credentials = WhitebitReadCredentials(
            api_key="private-api-key", api_secret="private-api-secret"
        )
        rendered = repr(credentials)
        self.assertNotIn("private-api-key", rendered)
        self.assertNotIn("private-api-secret", rendered)
        self.assertEqual(credentials.authority, "READ_ONLY")

    def test_interactive_store_never_accepts_or_echoes_secrets(self):
        from tools import configure_whitebit

        recorded = []
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(
                configure_whitebit,
                "getpass",
                side_effect=["private-api-key", "private-api-secret"],
            ),
            patch.object(
                configure_whitebit,
                "store_whitebit_read_credentials",
                side_effect=lambda **kwargs: recorded.append(kwargs),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(configure_whitebit.main(["store"]), 0)
            with self.assertRaises(SystemExit) as rejected:
                configure_whitebit.main(
                    ["store", "--api-key", "private-api-key"]
                )
        self.assertEqual(
            recorded,
            [
                {
                    "profile": "whitebit-primary",
                    "api_key": "private-api-key",
                    "api_secret": "private-api-secret",
                }
            ],
        )
        self.assertEqual(rejected.exception.code, 2)
        self.assertNotIn("private-api-key", stdout.getvalue())
        self.assertNotIn("private-api-key", stderr.getvalue())
        self.assertNotIn("private-api-secret", stdout.getvalue())
        self.assertNotIn("private-api-secret", stderr.getvalue())

    def test_connection_test_performs_only_both_balance_reads(self):
        from tools import configure_whitebit

        events = []

        class Reader:
            def __init__(self, api_key, api_secret, *, enable_network):
                events.append(("init", api_key, api_secret, enable_network))

            def __enter__(self):
                return self

            def __exit__(self, *_):
                events.append(("close",))

            def main_balance(self):
                events.append(("main_balance",))

            def trading_balance(self):
                events.append(("trading_balance",))

        stdout = io.StringIO()
        with (
            patch.object(
                configure_whitebit,
                "resolve_whitebit_read_credentials",
                return_value=WhitebitReadCredentials("key", "secret"),
            ),
            patch.object(configure_whitebit, "WhitebitPrivateReader", Reader),
            redirect_stdout(stdout),
        ):
            self.assertEqual(configure_whitebit.main(["test"]), 0)
        self.assertEqual(
            events,
            [
                ("init", "key", "secret", True),
                ("main_balance",),
                ("trading_balance",),
                ("close",),
            ],
        )
        self.assertNotIn("key", stdout.getvalue())
        self.assertNotIn("secret", stdout.getvalue())

    def test_connection_failure_does_not_echo_secret_exception(self):
        from tools import configure_whitebit

        stderr = io.StringIO()
        with (
            patch.object(
                configure_whitebit,
                "resolve_whitebit_read_credentials",
                side_effect=RuntimeError("private-api-secret"),
            ),
            redirect_stderr(stderr),
        ):
            self.assertEqual(configure_whitebit.main(["test"]), 1)
        self.assertNotIn("private-api-secret", stderr.getvalue())

    def test_delete_uses_only_the_fixed_logical_profile(self):
        from tools import configure_whitebit

        calls = []
        with (
            patch.object(
                configure_whitebit,
                "delete_whitebit_read_credentials",
                side_effect=lambda **kwargs: calls.append(kwargs),
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(configure_whitebit.main(["delete"]), 0)
        self.assertEqual(calls, [{"profile": "whitebit-primary"}])

    def test_credential_module_imports_under_python_without_site_packages(self):
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys;"
            + f"sys.path.insert(0,{str(root / 'mvp')!r});"
            + "from autotrade_mvp.whitebit_credentials import _target;"
            + "print(_target())"
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            "AutoTrade/ProviderRead/v1/whitebit/whitebit-primary",
        )


if __name__ == "__main__":
    unittest.main()
