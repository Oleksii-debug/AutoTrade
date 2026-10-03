from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import tools.build_windows_product as windows_product_module
from tools.build_windows_product import stage_windows_product_source
from tools.stage_windows_foundation import FoundationStagingError
from tools.stage_windows_host import _HOST_REQUIRED


ROOT = Path(__file__).resolve().parents[2]


def _source_sha() -> str:
    value = subprocess.check_output(
        ("git", "rev-parse", "--verify", "HEAD"),
        cwd=ROOT,
        text=True,
    ).strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise RuntimeError("production-host oracle requires an exact Git commit")
    return value


def _stage_product(root: Path) -> Path:
    staging = root / "staging"
    staging.mkdir()
    composition = root / "composition.json"
    composition.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "product": "AutoTrade",
                "source_sha": _source_sha(),
                "components": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    stage_windows_product_source(
        staging=staging,
        composition_path=composition,
        source_sha=_source_sha(),
        source_root=ROOT,
    )
    return staging


def _isolated(staging: Path, script: str, *, extra_env: dict[str, str] | None = None):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["AUTOTRADE_STAGING"] = str(staging)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        (sys.executable, "-I", "-S", "-c", script),
        cwd=staging,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class ProductionHostRuntimePackagingTests(unittest.TestCase):
    def test_product_staging_pins_one_source_sha_across_all_layers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            composition = root / "composition.json"
            source_sha = _source_sha()
            composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": source_sha,
                        "components": [],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            real_foundation = windows_product_module.stage_windows_foundation

            def stage_foundation_then_retarget(**kwargs):
                result = real_foundation(**kwargs)
                document = json.loads(composition.read_text(encoding="utf-8"))
                document["source_sha"] = (
                    "0" * 40 if source_sha != "0" * 40 else "1" * 40
                )
                composition.write_text(
                    json.dumps(document, sort_keys=True),
                    encoding="utf-8",
                )
                return result

            with (
                patch.object(
                    windows_product_module,
                    "stage_windows_foundation",
                    side_effect=stage_foundation_then_retarget,
                ),
                self.assertRaisesRegex(
                    FoundationStagingError,
                    "composition source_sha does not match expected source_sha",
                ),
            ):
                stage_windows_product_source(
                    staging=staging,
                    composition_path=composition,
                    source_sha=source_sha,
                    source_root=ROOT,
                )

            self.assertFalse(
                (staging / "autotrade_runtime" / "artifacts" / "store.py").exists()
            )
            self.assertFalse(
                (staging / "mvp" / "autotrade_mvp" / "production_host.py").exists()
            )

    def test_product_bundle_stages_canonical_source_before_bundle(self):
        calls = []

        def staged(**kwargs):
            calls.append(("stage", kwargs))
            return ()

        def bundled(**kwargs):
            calls.append(("bundle", kwargs))
            return {"output": str(kwargs["output"])}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            composition = root / "composition.json"
            output = root / "product.zip"
            with (
                patch.object(
                    windows_product_module,
                    "stage_windows_product_source",
                    side_effect=staged,
                ),
                patch.object(
                    windows_product_module,
                    "build_bundle",
                    side_effect=bundled,
                ),
            ):
                result = windows_product_module.build_windows_product_bundle(
                    staging=staging,
                    output=output,
                    version="1.2.3",
                    source_sha="a" * 40,
                    mode="diagnostics",
                    composition_path=composition,
                    source_root=ROOT,
                )

        self.assertEqual([name for name, _ in calls], ["stage", "bundle"])
        self.assertEqual(calls[0][1]["source_sha"], "a" * 40)
        self.assertEqual(calls[1][1]["source_sha"], "a" * 40)
        self.assertEqual(calls[1][1]["composition_path"], composition)
        self.assertEqual(result["output"], str(output))

    def test_host_descriptor_set_is_reviewed_and_excludes_checkout_only_trees(self):
        paths = tuple(item.path for item in _HOST_REQUIRED)
        self.assertEqual(len(paths), 35)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("mvp/autotrade_mvp/production_host.py", paths)
        self.assertIn("mvp/autotrade_mvp/host_network.py", paths)
        self.assertIn("mvp/autotrade_mvp/persistence.py", paths)
        self.assertIn("mvp/autotrade_mvp/authority.py", paths)
        self.assertIn("mvp/autotrade_mvp/_generated_common_scalars.py", paths)
        self.assertFalse(any(path.startswith("research/") for path in paths))
        self.assertFalse(any(path.startswith("contracts/") for path in paths))

    def test_exact_staged_host_imports_and_instance_fence_run_without_checkout_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = _stage_product(root)
            self.assertFalse((staging / "research").exists())
            self.assertFalse((staging / "contracts").exists())
            journal = root / "journal.sqlite3"
            payload = json.dumps(
                {
                    "journal_path": str(journal.resolve()),
                    "account_id": "paper-account",
                    "environment": "PAPER",
                    "host_id": "host-a",
                    "bind_host": "127.0.0.1",
                    "bind_port": 8765,
                    "public_origin": "http://127.0.0.1:8765",
                },
                separators=(",", ":"),
            )
            script = r"""
import os
from pathlib import Path
import sys

assert sys.flags.isolated == 1 and sys.flags.no_site == 1
staging = Path(os.environ["AUTOTRADE_STAGING"]).resolve(strict=True)
sys.path.insert(0, str(staging))

from mvp.autotrade_mvp.production_host import (
    _InstanceFence,
    parse_production_host_config,
)

assert "mvp.autotrade_mvp.pipeline" not in sys.modules
assert not any(name == "research" or name.startswith("research.") for name in sys.modules)
assert not any(name == "contracts" or name.startswith("contracts.") for name in sys.modules)

config = parse_production_host_config(
    os.environ["AUTOTRADE_HOST_CONFIG"].encode("utf-8")
)
fence = _InstanceFence.acquire(config.journal_path)
try:
    assert fence.path == Path(str(config.journal_path) + ".host.lock")
finally:
    fence.release()

for name, module in tuple(sys.modules.items()):
    if (
        name == "mvp"
        or name.startswith("mvp.")
        or name == "autotrade_foundation"
        or name.startswith("autotrade_foundation.")
        or name == "autotrade_numeric"
        or name.startswith("autotrade_numeric.")
        or name == "autotrade_runtime"
        or name.startswith("autotrade_runtime.")
    ):
        module_file = getattr(module, "__file__", None)
        if module_file is not None:
            assert Path(module_file).resolve(strict=True).is_relative_to(staging), (
                name,
                module_file,
            )
print("STAGED_PRODUCTION_HOST_OK")
"""
            completed = _isolated(
                staging,
                script,
                extra_env={"AUTOTRADE_HOST_CONFIG": payload},
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertEqual(completed.stdout.strip(), "STAGED_PRODUCTION_HOST_OK")
            self.assertFalse(journal.exists())

    def test_missing_runtime_lock_authority_fails_without_site_or_checkout_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = _stage_product(root)
            (staging / "autotrade_runtime" / "resource_lock.py").unlink()
            script = r"""
import os
from pathlib import Path
import sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
staging = Path(os.environ["AUTOTRADE_STAGING"]).resolve(strict=True)
sys.path.insert(0, str(staging))
try:
    from mvp.autotrade_mvp.production_host import ProductionHostConfig
except ModuleNotFoundError as error:
    assert error.name == "autotrade_runtime.resource_lock", error.name
    print("MISSING_RUNTIME_LOCK_DENIED")
else:
    raise AssertionError("production host imported without runtime lock authority")
"""
            completed = _isolated(staging, script)
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertEqual(completed.stdout.strip(), "MISSING_RUNTIME_LOCK_DENIED")

    def test_missing_packaged_scalar_authority_fails_without_contracts_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = _stage_product(root)
            (staging / "mvp" / "autotrade_mvp" / "_generated_common_scalars.py").unlink()
            self.assertFalse((staging / "contracts").exists())
            script = r"""
import os
from pathlib import Path
import sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
staging = Path(os.environ["AUTOTRADE_STAGING"]).resolve(strict=True)
sys.path.insert(0, str(staging))
try:
    from mvp.autotrade_mvp.production_host import ProductionHostConfig
except ModuleNotFoundError as error:
    assert error.name == "mvp.autotrade_mvp._generated_common_scalars", error.name
    print("MISSING_PACKAGED_SCALAR_DENIED")
else:
    raise AssertionError("production host imported without packaged scalar authority")
assert not any(name == "contracts" or name.startswith("contracts.") for name in sys.modules)
"""
            completed = _isolated(staging, script)
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertEqual(
                completed.stdout.strip(),
                "MISSING_PACKAGED_SCALAR_DENIED",
            )


if __name__ == "__main__":
    unittest.main()
