from pathlib import Path
import json
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
XAML = ROOT / "src" / "AutoTrade.Desktop" / "MainWindow.xaml"
CODE = ROOT / "src" / "AutoTrade.Desktop" / "MainWindow.xaml.cs"
APP = ROOT / "src" / "AutoTrade.Desktop" / "App.xaml.cs"
CLIENT = ROOT / "src" / "AutoTrade.Desktop" / "EmergencyHostClient.cs"
AUTHENTICATED_CLIENT = ROOT / "src" / "AutoTrade.Desktop" / "AuthenticatedEmergencyHostClient.cs"
PROJECT = ROOT / "src" / "AutoTrade.Desktop" / "AutoTrade.Desktop.csproj"
WEB_POLICY = ROOT / "src" / "AutoTrade.Desktop" / "WebExperienceSecurityPolicy.cs"
OPENAPI = ROOT / "contracts" / "openapi" / "host-api.yaml"
COMMON_SCHEMA = ROOT / "contracts" / "jsonschema" / "common.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "dotnet-foundation.yml"


class DesktopSafetyShellContractTests(unittest.TestCase):
    def test_wpf_project_targets_windows_without_unreviewed_packages(self):
        project = ET.parse(PROJECT).getroot()
        text = PROJECT.read_text(encoding="utf-8")
        self.assertIn("<TargetFramework>net10.0-windows</TargetFramework>", text)
        self.assertIn("<UseWPF>true</UseWPF>", text)
        packages = project.findall(".//PackageReference")
        self.assertEqual(
            [(p.get("Include"), p.get("Version")) for p in packages],
            [
                ("Microsoft.Web.WebView2", "1.0.4191.47"),
                ("Velopack", "1.2.158"),
            ],
        )
        self.assertIn("<RestorePackagesWithLockFile>true</RestorePackagesWithLockFile>", text)
        self.assertEqual(project.tag, "Project")

    def test_embedded_web_policy_is_fail_closed_and_host_api_scoped(self):
        text = WEB_POLICY.read_text(encoding="utf-8")
        self.assertIn("AuthenticatedEmergencyHostClient.ValidateBaseUri", text)
        self.assertIn("using AutoTrade.Contracts;", text)
        self.assertIn('StatePath = "/" + HostApiRoutes.GetState', text)
        self.assertIn('CommandPath = "/" + HostApiRoutes.SubmitCommand', text)
        self.assertIn('EventPath = "/" + HostApiRoutes.StreamEvents', text)
        self.assertIn("IsSameHostOrigin(target)", text)
        self.assertIn("Guid.TryParseExact(operationId, \"D\"", text)
        self.assertIn('parsedOperationId.ToString("D")', text)
        self.assertIn("HostApiRoutes.GetOperation(RouteProbeOperationId)", text)
        self.assertIn("BuildCanonicalOperationPrefix()", text)
        self.assertNotIn('"/api/v1/operations/"', text)
        self.assertIn("HasCanonicalEventQuery(target.Query)", text)
        self.assertIn('const string prefix = "?after=";', text)
        self.assertIn("value.Contains('&')", text)
        self.assertIn('string.Equals(method, "GET", StringComparison.Ordinal)', text)
        self.assertIn('string.Equals(method, "POST", StringComparison.Ordinal)', text)
        self.assertNotIn("CanonicalApiRoot", text)
        self.assertIn("AllowsWebMessageCommandAuthority => false", text)
        self.assertIn("AllowsDeveloperTools => false", text)
        self.assertIn("AllowsServiceWorkers => false", text)
        self.assertIn("AllowsDownloads => false", text)
        self.assertIn("AllowsNewWindow(Uri target) => false", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("AutoTrade-Session", text)

    def test_openapi_distinguishes_ephemeral_owned_desktop_pairing(self):
        text = OPENAPI.read_text(encoding="utf-8")
        session = text.split("  /api/v1/session:", 1)[1].split(
            "  /api/v1/state:",
            1,
        )[0]
        self.assertIn("Standalone ZERO launch", session)
        self.assertIn("owned WPF desktop child", session)
        self.assertIn("does not persist it", session)
        self.assertIn("Configured persistent Windows current-user session handoff failed", session)
        self.assertNotIn(
            "Windows ZERO launcher stores the same short-lived session in current-user Credential Manager",
            session,
        )

    def test_event_credential_query_grammar_matches_canonical_sequence_contract(self):
        policy = WEB_POLICY.read_text(encoding="utf-8")
        common = json.loads(COMMON_SCHEMA.read_text(encoding="utf-8"))
        sequence = common["$defs"]["Sequence"]

        self.assertEqual(sequence["type"], "string")
        self.assertEqual(
            sequence["pattern"],
            r"^(0|[1-9][0-9]*)$(?![\s\S])",
        )
        self.assertIn('const string prefix = "?after=";', policy)
        self.assertIn('if (value == "0")', policy)
        self.assertIn("value[0] is < '1' or > '9'", policy)
        self.assertIn("character is < '0' or > '9'", policy)
        self.assertIn("value.Contains('&')", policy)

    def test_wpf_uses_an_explicit_early_bootstrap_entrypoint(self):
        project_text = PROJECT.read_text(encoding="utf-8")
        app_text = APP.read_text(encoding="utf-8")
        self.assertIn(
            "<StartupObject>AutoTrade.Desktop.App</StartupObject>",
            project_text,
        )
        self.assertIn('<ApplicationDefinition Remove="App.xaml" />', project_text)
        self.assertIn('<Page Include="App.xaml" />', project_text)
        self.assertIn("[STAThread]", app_text)
        self.assertIn("private static void Main(string[] args)", app_text)
        self.assertLess(
            app_text.index("private static void Main(string[] args)"),
            app_text.index("protected override async void OnStartup"),
        )
        main_body = app_text.split(
            "private static void Main(string[] args)", 1
        )[1].split("protected override async void OnStartup", 1)[0]
        self.assertIn("using Velopack;", app_text)
        self.assertIn("VelopackApp.Build()", main_body)
        self.assertIn(".SetAutoApplyOnStartup(false)", main_body)
        self.assertIn(".Run();", main_body)
        self.assertIn("App app = new();", main_body)
        self.assertIn("app.InitializeComponent();", main_body)
        self.assertIn("app.Run();", main_body)
        self.assertLess(main_body.index("VelopackApp.Build()"), main_body.index("App app = new();"))
        self.assertNotIn("UpdateManager", main_body)
        self.assertNotIn("ApplyUpdates", main_body)
        self.assertNotIn("OnStartup(", main_body)

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

    def test_emergency_outcome_is_copyable_without_losing_assertive_live_semantics(self):
        text = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertNotIn('<TextBlock x:Name="EmergencyResult"', text)
        self.assertIn('<TextBox x:Name="EmergencyResult"', text)
        emergency = text.split('<TextBox x:Name="EmergencyResult"', 1)[1].split("/>", 1)[0]
        self.assertIn('IsReadOnly="True"', emergency)
        self.assertIn('IsReadOnlyCaretVisible="True"', emergency)
        self.assertIn('AutomationProperties.Name="Emergency command result"', emergency)
        self.assertIn('AutomationProperties.LiveSetting="Assertive"', emergency)
        self.assertIn("Use standard selection and copy commands.", emergency)
        self.assertIn("private void SetLiveRegionText(TextBox element, string text)", code)

    def test_status_refresh_is_keyboard_reachable_and_announced_without_focus_theft(self):
        text = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn('Content="_Refresh host status"', text)
        self.assertIn('AutomationProperties.Name="Refresh host status"', text)
        self.assertIn('AutomationProperties.LiveSetting="Polite"', text)
        self.assertIn("MainWindow_Loaded", text)
        self.assertIn("MainWindow_Closed", text)
        self.assertIn("returnFocus: false", code)
        self.assertIn(
            "returnFocus && RefreshStatusButton.IsKeyboardFocusWithin",
            code,
        )
        self.assertIn(
            "ReferenceEquals(focusedElement, RefreshStatusButton)",
            code,
        )
        self.assertNotIn("System.Windows.Input.IInputElement", code)
        self.assertIn("System.Windows.IInputElement?", code)
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
        self.assertGreaterEqual(code.count("HostStatusAnnouncement,"), 2)
        self.assertGreaterEqual(code.count("EmergencyResult,"), 6)

    def test_snapshot_busy_is_retryable_without_fabricating_disconnect(self):
        client = AUTHENTICATED_CLIENT.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("public sealed class EmergencySnapshotBusyException", client)
        self.assertIn("HttpStatusCode.ServiceUnavailable", client)
        self.assertIn('"SNAPSHOT_BUSY"', client)
        self.assertIn("retryable.ValueKind == JsonValueKind.True", client)
        self.assertIn("throw new EmergencySnapshotBusyException();", client)

        refresh = code[code.index("private async Task RefreshHostStatusAsync"):
                       code.index("private void ApplySnapshotBusyStatus")]
        busy_catch = refresh.index("catch (EmergencySnapshotBusyException)")
        generic_catch = refresh.index("catch (Exception)", busy_catch)
        self.assertLess(busy_catch, generic_catch)
        self.assertIn("ApplySnapshotBusyStatus(", refresh[busy_catch:generic_catch])

        busy = code[code.index("private void ApplySnapshotBusyStatus"):
                    code.index("private void ApplyHostStatus")]
        self.assertIn("_lastKnownConnectedStatus", busy)
        self.assertIn("(stale)", busy)
        self.assertIn("temporarily busy", busy)
        self.assertIn("Retry is safe.", busy)
        self.assertNotIn("EmergencyHostStatus.Disconnected(", busy)

    def test_emergency_snapshot_busy_is_before_send_and_not_uncertain_acceptance(self):
        code = CODE.read_text(encoding="utf-8")
        action = code[code.index("private async void BlockNewExposure_Click"):]
        busy = action.index("catch (EmergencySnapshotBusyException)")
        uncertain = action.index("catch (EmergencyCommandUncertainException", busy)
        generic = action.index("catch (Exception)", uncertain)
        self.assertLess(busy, uncertain)
        self.assertLess(uncertain, generic)
        busy_body = action[busy:uncertain]
        self.assertIn("No emergency command was created or sent", busy_body)
        self.assertIn("no durable block has been confirmed", busy_body)
        self.assertIn("Retry the same Block new exposure action", busy_body)
        self.assertNotIn("uncertain.CommandId", busy_body)

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
        self.assertIn(
            "BlockNewExposureButton.IsKeyboardFocusWithin",
            code,
        )
        self.assertIn(
            "ReferenceEquals(focusedElement, BlockNewExposureButton)",
            code,
        )

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

    def test_default_zero_host_uses_same_current_user_pairing_target_as_launcher(self):
        client = AUTHENTICATED_CLIENT.read_text(encoding="utf-8")
        self.assertIn(
            'new Uri("http://127.0.0.1:8765/", UriKind.Absolute)',
            client,
        )
        self.assertIn(
            "WindowsCredentialManagerSessionProvider.CredentialTargetForOrigin(uri)",
            client,
        )
        self.assertIn(
            'Environment.GetEnvironmentVariable("AUTOTRADE_HOST_URI")',
            client,
        )
        self.assertIn(
            'Environment.GetEnvironmentVariable("AUTOTRADE_HOST_CREDENTIAL_TARGET")',
            client,
        )
        self.assertIn("UseCookies = false", client)
        self.assertIn("AllowAutoRedirect = false", client)

    def test_windows_ci_builds_the_actual_desktop_project(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('src/**/*.xaml', workflow)
        self.assertIn("desktop-build:", workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn(
            "dotnet tool restore --tool-manifest .config/dotnet-tools.json",
            workflow,
        )
        self.assertIn(
            "dotnet restore src/AutoTrade.Desktop/AutoTrade.Desktop.csproj --locked-mode",
            workflow,
        )
        self.assertIn(
            "dotnet restore tests/Desktop.Client/Desktop.Client.csproj --locked-mode",
            workflow,
        )
        self.assertIn(
            "dotnet build src/AutoTrade.Desktop/AutoTrade.Desktop.csproj",
            workflow,
        )


    def test_owned_runtime_drains_redirected_child_pipes_without_unbounded_capture(self):
        code = (ROOT / "src" / "AutoTrade.Desktop" / "OwnedProviderFreeRuntime.cs").read_text(encoding="utf-8")
        self.assertIn("DrainRedirectedPipeAsync", code)
        self.assertIn("Task stdoutDrain = Task.CompletedTask;", code)
        self.assertIn("Task stderrDrain = DrainRedirectedPipeAsync(process.StandardError);", code)
        self.assertIn("stdoutDrain = DrainRedirectedPipeAsync(process.StandardOutput);", code)
        self.assertIn("await Task.WhenAll(_stdoutDrain, _stderrDrain);", code)
        self.assertNotIn("ReadToEndAsync", code)


if __name__ == "__main__":
    unittest.main()
