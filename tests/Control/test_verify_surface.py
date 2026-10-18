import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_verify_module():
    path = ROOT / "tools" / "verify.py"
    spec = importlib.util.spec_from_file_location("autotrade_verify_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VerifySurfaceTests(unittest.TestCase):
    def test_core_verify_runs_cross_language_contract_generation_and_consumers(self):
        module = load_verify_module()
        linux = {" ".join(command) for command in module.verification_commands(platform="linux")}
        windows = {" ".join(command) for command in module.verification_commands(platform="win32")}

        for commands in (linux, windows):
            self.assertTrue(
                any(
                    "tools/generate_common_scalar_bindings.py --check" in command
                    for command in commands
                ),
                "Core verification omitted generated scalar binding drift detection",
            )
            self.assertTrue(
                any(
                    "tools/generate_common_scalar_corpus.py --check" in command
                    for command in commands
                ),
                "Core verification omitted scalar corpus drift detection",
            )
            self.assertIn(
                "node tests/Contracts.TypeScript/common-scalars.test.cjs",
                commands,
                "Core verification omitted the TypeScript canonical-contract consumer",
            )
            self.assertTrue(
                any(
                    "tests/Contracts.DotNet/Contracts.DotNet.csproj" in command
                    for command in commands
                ),
                "Core verification omitted the .NET canonical-contract executable",
            )

    def test_core_verify_runs_windows_desktop_only_on_windows(self):
        module = load_verify_module()
        linux = {" ".join(command) for command in module.verification_commands(platform="linux")}
        windows = {" ".join(command) for command in module.verification_commands(platform="win32")}

        self.assertFalse(
            any("tests/Desktop.Client/Desktop.Client.csproj" in command for command in linux),
            "Linux core verification must not run the Windows/WPF desktop executable",
        )
        self.assertTrue(
            any("tests/Desktop.Client/Desktop.Client.csproj" in command for command in windows),
            "Windows core verification omitted the desktop authenticated-host contract executable",
        )

    def test_verify_workflow_does_not_claim_separate_qualifications(self):
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--suite full-repository", workflow)
        self.assertIn("--suite core-repository", workflow)
        self.assertIn('--command "python tools/verify.py"', workflow)
        self.assertNotIn("python tools/verify.py; LEAN/provider", workflow)

    def test_verify_workflow_pins_node_for_typescript_contract_consumer(self):
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("actions/setup-node@v4", workflow)
        self.assertIn('node-version: "22.23.3"', workflow)
        self.assertNotIn('node-version: "22"\n', workflow)


if __name__ == "__main__":
    unittest.main()
