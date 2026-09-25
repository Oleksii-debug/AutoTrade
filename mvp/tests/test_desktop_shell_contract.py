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

    def test_live_regions_raise_automation_events_without_moving_focus(self):
        text = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertNotIn('TextChanged="LiveRegion_TextChanged"', text)
        self.assertIn("private void SetLiveRegionText(TextBlock element, string text)", code)
        self.assertIn("UIElementAutomationPeer.FromElement(element)", code)
        self.assertIn("UIElementAutomationPeer.CreatePeerForElement(element)", code)
        self.assertIn(
            "RaiseAutomationEvent(AutomationEvents.LiveRegionChanged)",
            code,
        )
        handler = code.split("private void SetLiveRegionText", 1)[1].split(
            "private async void MainWindow_Loaded", 1
        )[0]
        self.assertNotIn(".Focus()", handler)
        self.assertIn("if (!IsLoaded)", handler)
        self.assertGreaterEqual(code.count("SetLiveRegionText(HostStatusAnnouncement"), 2)
        self.assertGreaterEqual(code.count("SetLiveRegionText(EmergencyResult"), 6)

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

    def test_connected_host_status_is_validated_before_becoming_current_evidence(self):
        client = CLIENT.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("public EmergencyHostStatus Validated()", client)
        self.assertIn("Host status evidence time must be a non-default UTC instant.", client)
        self.assertIn("Connected host status requires canonical", client)
        self.assertIn("StringComparison.OrdinalIgnoreCase", client)
        self.assertIn(").Validated();", code)
        self.assertLess(code.index(").Validated();"), code.index("if (status.Connected)"))

    def test_future_host_status_cannot_be_presented_as_current_evidence(self):
        client = CLIENT.read_text(encoding="utf-8")
        self.assertIn("ObservedAtUtc > DateTimeOffset.UtcNow", client)
        self.assertIn(
            "Host status evidence time cannot be in the future.",
            client,
        )

    def test_connected_environment_and_state_version_use_canonical_contracts(self):
        client = CLIENT.read_text(encoding="utf-8")
        self.assertIn(
            'value is not ("REPLAY" or "SIMULATION" or "PAPER" or "LIVE")',
            client,
        )
        self.assertIn(
            "Connected host state version must be a canonical non-negative integer sequence string.",
            client,
        )
        self.assertIn('value == "0"', client)
        self.assertIn("value[0] is >= '1' and <= '9'", client)
        self.assertIn("foreach (char character in value)", client)
        self.assertIn("character is < '0' or > '9'", client)

    def test_connected_host_evidence_cannot_regress_or_switch_authority_silently(self):
        client = CLIENT.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("ValidateSuccessorOf", client)
        self.assertIn("Connected host state version cannot regress.", client)
        self.assertIn("Connected host evidence time cannot regress.", client)
        self.assertIn(
            "Connected host authority identity changed without an explicit transition.",
            client,
        )
        self.assertIn("CompareCanonicalSequence", client)
        self.assertIn("status.ValidateSuccessorOf(previous)", code)
        self.assertLess(
            code.index("status.ValidateSuccessorOf(previous)"),
            code.index("_lastKnownConnectedStatus = status"),
        )

    def test_accepted_emergency_operation_requires_canonical_uuid_identity(self):
        client = CLIENT.read_text(encoding="utf-8")
        self.assertIn("Guid.TryParse", client)
        self.assertIn('parsedOperationId.ToString("D")', client)
        self.assertIn(
            "An accepted emergency request requires a canonical UUID operation identity.",
            client,
        )
        self.assertIn('string canonicalOperationId = "Unavailable";', client)
        self.assertIn("OperationId = canonicalOperationId;", client)

    def test_accepted_emergency_operation_has_same_identity_recovery_contract(self):
        client = CLIENT.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("EmergencyOperationState", client)
        self.assertIn("EmergencyOperationStatus", client)
        self.assertIn("Task<EmergencyOperationStatus> GetOperationAsync(", client)
        self.assertIn(
            "A durable block may be confirmed only by a succeeded emergency operation.",
            client,
        )
        self.assertIn(
            "A succeeded emergency operation must carry durable block confirmation.",
            client,
        )
        self.assertIn(
            "await _hostClient.GetOperationAsync(operationId, _lifetime.Token)",
            code,
        )
        self.assertIn(
            "Recovered emergency operation identity does not match the accepted operation.",
            code,
        )
        self.assertIn(
            "Do not resubmit with a new idempotency identity",
            code,
        )
        self.assertIn(
            "recover this same operation",
            code.lower(),
        )

    def test_disconnected_operation_recovery_remains_unknown_and_does_not_resubmit(self):
        client = CLIENT.read_text(encoding="utf-8")
        self.assertIn("public Task<EmergencyOperationStatus> GetOperationAsync(", client)
        self.assertIn("state: EmergencyOperationState.Unknown", client)
        self.assertIn("durableBlockConfirmed: false", client)
        self.assertIn("inFlightActions: InFlightActionState.Unknown", client)
        recovery_body = client.split(
            "public Task<EmergencyOperationStatus> GetOperationAsync(", 1
        )[1]
        self.assertNotIn("BlockNewExposureAsync(", recovery_body)

    def test_emergency_control_is_named_keyboard_reachable_and_truthful(self):
        text = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        client = CLIENT.read_text(encoding="utf-8")
        self.assertIn('Content="_Block new exposure"', text)
        self.assertIn('AutomationProperties.Name="Block new exposure"', text)
        self.assertIn('AutomationProperties.LiveSetting="Assertive"', text)
        self.assertIn("does not mean existing provider orders are cancelled", text)
        self.assertIn('AutomationProperties.Name="Emergency operation"', text)
        self.assertIn("DurableBlockConfirmed", client)
        self.assertIn("InFlightActionState", client)
        self.assertIn("OperationId", client)
        self.assertIn("No durable block has been confirmed", code)
        self.assertIn("durable block is not yet confirmed", code)
        self.assertIn("Durable block confirmed by the host", code)
        self.assertIn("Outstanding in-flight actions", code)

    def test_disconnected_default_never_fabricates_host_state_or_acceptance(self):
        client = CLIENT.read_text(encoding="utf-8")
        self.assertIn("EmergencyHostStatus.Disconnected(", client)
        self.assertIn("new EmergencyCommandResult(", client)
        self.assertIn("accepted: false", client)
        self.assertIn("durableBlockConfirmed: false", client)
        self.assertIn("InFlightActionState.Unknown", client)
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
