from pathlib import Path
import json
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
XAML = ROOT / "src" / "AutoTrade.Desktop" / "MainWindow.xaml"
CODE = ROOT / "src" / "AutoTrade.Desktop" / "MainWindow.xaml.cs"
APP = ROOT / "src" / "AutoTrade.Desktop" / "App.xaml.cs"
CLIENT = ROOT / "src" / "AutoTrade.Desktop" / "EmergencyHostClient.cs"
AUTH_CLIENT = ROOT / "src" / "AutoTrade.Desktop" / "AuthenticatedEmergencyHostClient.cs"
PROJECT = ROOT / "src" / "AutoTrade.Desktop" / "AutoTrade.Desktop.csproj"
WEB_POLICY = ROOT / "src" / "AutoTrade.Desktop" / "WebExperienceSecurityPolicy.cs"
COMMON_SCHEMA = ROOT / "contracts" / "jsonschema" / "common.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "dotnet-foundation.yml"


class DesktopSafetyShellContractTests(unittest.TestCase):
    def test_wpf_project_targets_windows_with_only_exact_admitted_webview2_package(self):
        project = ET.parse(PROJECT).getroot()
        text = PROJECT.read_text(encoding="utf-8")
        self.assertIn("<TargetFramework>net10.0-windows</TargetFramework>", text)
        self.assertIn("<UseWPF>true</UseWPF>", text)
        package_refs = project.findall(".//PackageReference")
        self.assertEqual(len(package_refs), 1)
        self.assertEqual(package_refs[0].attrib.get("Include"), "Microsoft.Web.WebView2")
        self.assertEqual(package_refs[0].attrib.get("Version"), "1.0.4258.31")
        self.assertEqual(project.tag, "Project")

    def test_primary_webview2_surface_is_real_and_native_safety_remains_independent(self):
        xaml = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn('x:Name="ProductWebViewHost"', xaml)
        self.assertIn("new WebView2", code)
        self.assertIn("AutomationProperties.SetName(", code)
        self.assertIn('"AutoTrade application web interface"', code)
        self.assertIn('Content="_Focus application web interface"', xaml)
        self.assertIn('AutomationProperties.Name="Block new exposure"', xaml)
        self.assertIn('AutomationProperties.Name="Host connection status"', xaml)
        self.assertIn("await ConnectWebExperienceAsync();", code)
        self.assertIn("ProductWebViewHost.Visibility = Visibility.Visible;", code)
        self.assertIn("FocusWebButton.IsEnabled = true;", code)

    def test_window_access_keys_are_unique(self):
        root = ET.parse(XAML).getroot()
        access_keys = []
        for element in root.iter():
            content = element.attrib.get("Content")
            if not content:
                continue
            marker = content.find("_")
            if marker < 0 or marker + 1 >= len(content):
                continue
            access_keys.append((content[marker + 1].casefold(), content))

        key_names = [key for key, _ in access_keys]
        self.assertEqual(
            len(key_names),
            len(set(key_names)),
            "Window access keys must be unique: " + repr(access_keys),
        )
        self.assertIn(("w", "Reload application _web interface"), access_keys)

    def test_default_window_construction_keeps_native_and_web_on_one_connection(self):
        code = CODE.read_text(encoding="utf-8")
        constructor = code.split("public MainWindow()", 1)[1].split(
            "internal MainWindow(", 1
        )[0]
        self.assertIn("DesktopHostClientFactory.CreateConnection()", constructor)
        self.assertIn("private MainWindow(DesktopHostConnection connection)", constructor)
        self.assertIn("connection.Client", constructor)
        self.assertIn("connection.SessionProvider", constructor)
        self.assertNotIn("DesktopHostClientFactory.Create()", constructor)

    def test_native_status_refresh_precedes_optional_webview_startup(self):
        code = CODE.read_text(encoding="utf-8")
        loaded = code.split(
            "private async void MainWindow_Loaded", 1
        )[1].split("private async Task ConnectWebExperienceAsync", 1)[0]
        self.assertLess(
            loaded.index("RefreshHostStatusAsync"),
            loaded.index("ConnectWebExperienceAsync"),
        )
        self.assertIn("if (!_lifetime.IsCancellationRequested)", loaded)

    def test_webview2_security_events_delegate_to_shared_policy_and_fail_closed(self):
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("WebExperienceSecurityPolicy policy = new(origin);", code)
        self.assertIn("core.Settings.AreDevToolsEnabled = policy.AllowsDeveloperTools;", code)
        self.assertIn(
            "core.Settings.IsWebMessageEnabled = policy.AllowsWebMessageCommandAuthority;",
            code,
        )
        self.assertIn("core.FrameNavigationStarting += (_, e) => e.Cancel = true;", code)
        self.assertIn("core.NewWindowRequested += (_, e) => e.Handled = true;", code)
        self.assertIn("core.DownloadStarting += (_, e) => e.Cancel = true;", code)
        self.assertIn("CoreWebView2PermissionState.Deny", code)
        self.assertIn("CoreWebView2ServerCertificateErrorAction.Cancel", code)
        self.assertIn("policy.AllowsTopLevelNavigation(target)", code)
        self.assertIn("policy.AllowsSessionHeaderForwarding(", code)
        self.assertIn("CoreWebView2WebResourceRequestSourceKinds.Document", code)
        self.assertIn("CoreWebView2WebResourceContext.Fetch", code)
        self.assertIn("CoreWebView2WebResourceContext.XmlHttpRequest", code)
        self.assertIn("_trustedWebDocumentActive", code)

    def test_webview2_old_generation_callbacks_cannot_mutate_new_browser_authority(self):
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("private long _webGeneration;", code)
        self.assertIn("long generation = ++_webGeneration;", code)
        self.assertIn("generation == _webGeneration", code)
        self.assertIn("ReferenceEquals(webView, _productWebView)", code)
        self.assertIn("_webGeneration++;", code)
        self.assertIn("if (!IsCurrentWebGeneration(webView, generation))", code)
        self.assertIn(
            "WebView_ProcessFailed(webView, generation, e)",
            code,
        )
        resource = code.split("core.WebResourceRequested += (_, e) =>", 1)[1].split(
            "core.Navigate(origin.AbsoluteUri)", 1
        )[0]
        self.assertLess(
            resource.index('e.Request.Headers.RemoveHeader("Authorization")'),
            resource.index("IsCurrentWebGeneration(webView, generation)"),
        )
        self.assertLess(
            resource.index("IsCurrentWebGeneration(webView, generation)"),
            resource.index('e.Request.Headers.SetHeader(\n                        "Authorization"'),
        )

    def test_webview2_async_startup_cannot_create_or_leak_browser_after_window_close(self):
        code = CODE.read_text(encoding="utf-8")
        connect = code.split("private async Task ConnectWebExperienceAsync", 1)[1].split(
            "private static bool RequiresFreshWebViewAfterFailure", 1
        )[0]
        environment = connect.index("await CoreWebView2Environment.CreateAsync")
        cancellation_after_environment = connect.index(
            "if (_lifetime.IsCancellationRequested || !IsLoaded)",
            environment,
        )
        create_control = connect.index("webView = new WebView2", cancellation_after_environment)
        self.assertLess(cancellation_after_environment, create_control)
        ensure = connect.index("await webView.EnsureCoreWebView2Async", create_control)
        cancellation_after_ensure = connect.index(
            "if (_lifetime.IsCancellationRequested || !IsLoaded)",
            ensure,
        )
        cleanup_after_ensure = connect.index("DisposeWebExperience();", cancellation_after_ensure)
        clear = connect.index("await core.Profile.ClearBrowsingDataAsync", cleanup_after_ensure)
        cancellation_after_clear = connect.index(
            "if (_lifetime.IsCancellationRequested || !IsLoaded)",
            clear,
        )
        cleanup_after_clear = connect.index("DisposeWebExperience();", cancellation_after_clear)
        self.assertLess(ensure, cancellation_after_ensure)
        self.assertLess(cancellation_after_ensure, cleanup_after_ensure)
        self.assertLess(clear, cancellation_after_clear)
        self.assertLess(cancellation_after_clear, cleanup_after_clear)

    def test_webview2_process_failure_revokes_trust_and_recreates_control_outside_handler(self):
        xaml = XAML.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn('Content="_Reload application web interface"', xaml)
        self.assertIn('AutomationProperties.Name="Reload application web interface"', xaml)
        self.assertIn(
            "core.ProcessFailed += (_, e) =>\n                WebView_ProcessFailed(webView, generation, e);",
            code,
        )
        self.assertIn("CoreWebView2ProcessFailedKind.BrowserProcessExited", code)
        self.assertIn("CoreWebView2ProcessFailedKind.RenderProcessExited", code)
        self.assertIn("CoreWebView2ProcessFailedKind.RenderProcessUnresponsive", code)
        self.assertIn("_trustedWebDocumentActive = false", code)
        self.assertIn("ProductWebViewHost.Visibility = Visibility.Collapsed", code)
        self.assertIn("ReloadWeb_Click", code)
        self.assertIn("DisposeWebExperience();", code)
        self.assertIn("webView?.Dispose();", code)
        self.assertIn("await ConnectWebExperienceAsync();", code)
        handler = code.split("private void WebView_ProcessFailed", 1)[1].split(
            "private void DisposeWebExperience", 1
        )[0]
        self.assertNotIn("DisposeWebExperience()", handler)
        self.assertNotIn("ConnectWebExperienceAsync()", handler)

    def test_webview2_session_never_becomes_a_cookie_and_stale_browser_authority_is_purged(self):
        code = CODE.read_text(encoding="utf-8")
        self.assertNotIn('CreateCookie("AutoTradeSession"', code)
        self.assertNotIn("Cookie = ", code)
        self.assertIn("core.CookieManager.DeleteAllCookies();", code)
        self.assertIn("CoreWebView2BrowsingDataKinds.ServiceWorkers", code)
        self.assertIn('e.Request.Headers.RemoveHeader("Authorization")', code)
        self.assertIn('e.Request.Headers.RemoveHeader("X-AutoTrade-Actor")', code)

    def test_webview2_reuses_the_same_paired_session_authority_as_native_host_client(self):
        auth = AUTH_CLIENT.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")
        code = CODE.read_text(encoding="utf-8")
        self.assertIn("internal sealed record DesktopHostConnection(", auth)
        self.assertIn("WindowsCredentialManagerSessionProvider sessionProvider = new(", auth)
        self.assertIn("new AuthenticatedEmergencyHostClient(", auth)
        self.assertIn("return new DesktopHostConnection(client, sessionProvider);", auth)
        self.assertIn("DesktopHostClientFactory.CreateConnection()", app)
        self.assertIn("connection.Client, connection.SessionProvider", app)
        self.assertIn("_sessionProvider.GetSession().Validated()", code)
        self.assertIn('"AutoTrade-Session " + currentSession.Token', code)
        self.assertIn('"X-AutoTrade-Actor",\n                        currentSession.Actor', code)
        self.assertNotIn('"X-AutoTrade-Actor", "local-owner"', code)

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
            app_text.index("protected override void OnStartup"),
        )
        main_body = app_text.split(
            "private static void Main(string[] args)", 1
        )[1].split("protected override void OnStartup", 1)[0]
        self.assertIn("App app = new();", main_body)
        self.assertIn("app.InitializeComponent();", main_body)
        self.assertIn("app.Run();", main_body)
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
        self.assertGreaterEqual(code.count("HostStatusAnnouncement,"), 2)
        self.assertGreaterEqual(code.count("EmergencyResult,"), 6)

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
