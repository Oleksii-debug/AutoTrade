from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
XAML = ROOT / "src" / "AutoTrade.Desktop" / "MainWindow.xaml"
CODE = ROOT / "src" / "AutoTrade.Desktop" / "MainWindow.xaml.cs"
CLIENT = ROOT / "src" / "AutoTrade.Desktop" / "EmergencyHostClient.cs"
PROJECT = ROOT / "src" / "AutoTrade.Desktop" / "AutoTrade.Desktop.csproj"
WORKFLOW = ROOT / ".github" / "workflows" / "dotnet-foundation.yml"


class DesktopSafetyShellContractTests(unittest.TestCase):
    def test_wpf_project_targets_windows_without_unreviewed_packages(self):
        project = ET.parse(PROJECT).getroot()
        text = PROJECT.read_text(encoding="utf-8")
        self.assertIn("<TargetFramework>net10.0-windows</TargetFramework>", text)
        self.assertIn("<UseWPF>true</UseWPF>", text)
        self.assertNotIn("<PackageReference", text)
        self.assertEqual(project.tag, "Project")

    def test_native_surface_exposes_copyable_host_account_environment_and_evidence(self):
        text = XAML.read_text(encoding="utf-8")
        for name in (
            "Active host",
            "Active account",
            "Active environment",
            "Host state version",
            "Last host evidence time",
        ):
            self.assertIn(f'AutomationProperties.Name="{name}"', text)
        self.assertGreaterEqual(text.count('IsReadOnly="True"'), 5)
        self.assertIn('Target="{Binding ElementName=HostValue}"', text)
        self.assertIn('Target="{Binding ElementName=AccountValue}"', text)
        self.assertIn('Target="{Binding ElementName=EnvironmentValue}"', text)
        self.assertIn('Target="{Binding ElementName=StateVersionValue}"', text)
        self.assertIn('Target="{Binding ElementName=LastEvidenceValue}"', text)

    def test_status_refresh_is_keyboard_reachable_and_announced_without_focus_theft(self):
        text = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn('Content="_Refresh host status"', text)
        self.assertIn('AutomationProperties.Name="Refresh host status"', text)
        self.assertIn('AutomationProperties.LiveSetting="Polite"', text)
        self.assertIn("MainWindow_Loaded", text)
        self.assertIn("MainWindow_Closed", text)
        self.assertIn("returnFocus: false", code)
        self.assertIn("_lifetime.Cancel()", code)

    def test_failed_refresh_preserves_last_known_values_as_stale(self):
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("_lastKnownConnectedStatus", code)
        self.assertIn("(stale)", code)
        self.assertIn(
            "Last known host values are stale and are not current evidence.",
            code,
        )
        self.assertIn(
            "No cancellation, flattening, or provider outcome is implied.",
            code,
        )

    def test_emergency_control_is_named_keyboard_reachable_and_truthful(self):
        text = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn('Content="_Block new exposure"', text)
        self.assertIn('AutomationProperties.Name="Block new exposure"', text)
        self.assertIn('AutomationProperties.LiveSetting="Assertive"', text)
        self.assertIn("does not mean existing provider orders are cancelled", text)
        self.assertIn("No durable block has been confirmed", code)
        self.assertIn("provider and financial outcomes remain separately tracked", code)

    def test_disconnected_default_never_fabricates_host_state_or_acceptance(self):
        client = CLIENT.read_text(encoding="utf-8")
        self.assertIn("EmergencyHostStatus.Disconnected(", client)
        self.assertIn("new EmergencyCommandResult(", client)
        self.assertIn("false,", client)
        self.assertIn("No durable block of new exposure has been confirmed", client)
        self.assertNotIn("WITHDRAW", client.upper())
        self.assertNotIn("TRANSFER", client.upper())

    def test_windows_ci_builds_the_actual_desktop_project(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('src/**/*.xaml', workflow)
        self.assertIn("desktop-build:", workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn(
            "dotnet build src/AutoTrade.Desktop/AutoTrade.Desktop.csproj",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
