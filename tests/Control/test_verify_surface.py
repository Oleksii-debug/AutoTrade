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
    def test_core_verify_includes_dotnet_contracts_and_desktop(self):
        module = load_verify_module()
        commands = module.verification_commands()
        rendered = {" ".join(command) for command in commands}

        self.assertTrue(
            any("tests/Contracts.DotNet/Contracts.DotNet.csproj" in command for command in rendered),
            "core verification omitted the .NET canonical-contract executable",
        )
        self.assertTrue(
            any("tests/Desktop.Client/Desktop.Client.csproj" in command for command in rendered),
            "core verification omitted the desktop authenticated-host contract executable",
        )

    def test_verify_workflow_does_not_claim_separate_qualifications(self):
        workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--suite full-repository", workflow)
        self.assertIn("--suite core-repository", workflow)
        self.assertIn('--command "python tools/verify.py"', workflow)
        self.assertNotIn("python tools/verify.py; LEAN/provider", workflow)


if __name__ == "__main__":
    unittest.main()
