using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace AutoTrade.Desktop;

public partial class MainWindow : Window
{
    private readonly IEmergencyHostClient _hostClient;
    private readonly CancellationTokenSource _lifetime = new();
    private EmergencyHostStatus? _lastKnownConnectedStatus;
    private EmergencyHostStatus? _lastKnownCurrentStatus;
    private readonly AuthenticatedEmergencyHostClient? _authenticatedHostClient;
    private WebView2? _webView;
    private WebExperienceSecurityPolicy? _webSecurityPolicy;
    private Uri? _trustedTopLevelDocument;

    public MainWindow()
        : this(DesktopHostClientFactory.Create())
    {
    }

    internal MainWindow(IEmergencyHostClient hostClient)
    {
        _hostClient = hostClient ?? throw new ArgumentNullException(nameof(hostClient));
        _authenticatedHostClient = hostClient as AuthenticatedEmergencyHostClient;
        InitializeComponent();
        ConnectionStatus.Text = "Host unavailable; new exposure cannot be confirmed blocked from this window.";
    }

    private void SetLiveRegionText(TextBlock element, string text)
    {
        element.Text = text;
        if (!IsLoaded)
        {
            return;
        }

        AutomationPeer? peer =
            UIElementAutomationPeer.FromElement(element)
            ?? UIElementAutomationPeer.CreatePeerForElement(element);
        peer?.RaiseAutomationEvent(AutomationEvents.LiveRegionChanged);
    }

    private async void MainWindow_Loaded(object sender, RoutedEventArgs e)
    {
        await RefreshHostStatusAsync(announce: true, returnFocus: false);
        await InitializeWebExperienceAsync();
    }

    private void MainWindow_Closed(object? sender, EventArgs e)
    {
        _lifetime.Cancel();
        DisposeWebExperience();
    }

    private async void RefreshHostStatus_Click(object sender, RoutedEventArgs e)
    {
        await RefreshHostStatusAsync(announce: true, returnFocus: true);
    }

    private async Task RefreshHostStatusAsync(bool announce, bool returnFocus)
    {
        RefreshStatusButton.IsEnabled = false;

        try
        {
            EmergencyHostStatus status = await _hostClient.GetStatusAsync(_lifetime.Token);
            ApplyHostStatus(status, announce);
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested)
        {
            return;
        }
        catch (Exception)
        {
            ApplyHostStatus(
                EmergencyHostStatus.Disconnected(
                    "Host refresh failed. No new host evidence was accepted."),
                announce);
        }
        finally
        {
            RefreshStatusButton.IsEnabled = true;
            if (returnFocus && IsLoaded)
            {
                RefreshStatusButton.Focus();
            }
        }
    }

    private void ApplyHostStatus(EmergencyHostStatus status, bool announce)
    {
        status = (status ?? throw new InvalidOperationException(
            "Host status response was null.")).Validated();

        if (status.Connected)
        {
            if (_lastKnownConnectedStatus is { } previous)
            {
                status = status.IsCurrent
                    ? status.ValidateSuccessorOf(previous)
                    : status.ValidateStaleSuccessorOf(previous);
            }

            // STALE snapshots can carry older evidence without lowering the
            // last accepted CURRENT floor. Validate both before updating either
            // memory; rejected refreshes and disconnects retain both fences.
            if (status.IsCurrent && _lastKnownCurrentStatus is { } lastCurrent)
            {
                status = status.ValidateSuccessorOf(lastCurrent);
            }

            // Preserve the newest validated connected authority snapshot even
            // when its freshness is non-current.  Freshness may make evidence
            // older, but it must never erase durable host/account/environment
            // identity or state-version monotonicity from the successor chain.
            _lastKnownConnectedStatus = status;
            if (status.IsCurrent)
            {
                _lastKnownCurrentStatus = status;
            }

            if (status.IsCurrent)
            {
                HostValue.Text = status.HostId;
                AccountValue.Text = status.AccountId;
                EnvironmentValue.Text = status.Environment;
                StateVersionValue.Text = status.StateVersion;
                LastEvidenceValue.Text = status.ObservedAtUtc.ToString("O");
                ConnectionStatus.Text = status.Message;
            }
            else
            {
                HostValue.Text = $"{status.HostId} (stale)";
                AccountValue.Text = $"{status.AccountId} (stale)";
                EnvironmentValue.Text = $"{status.Environment} (stale)";
                StateVersionValue.Text = $"{status.StateVersion} (stale)";
                LastEvidenceValue.Text = $"{status.ObservedAtUtc:O} (stale)";
                ConnectionStatus.Text =
                    $"{status.Message} Snapshot values are stale and are not current evidence.";
            }

            if (announce)
            {
                string prefix = status.IsCurrent
                    ? "Host status refreshed."
                    : "Host status is stale.";
                SetLiveRegionText(
                    HostStatusAnnouncement,
                    $"{prefix} {ConnectionStatus.Text} State version {status.StateVersion}. No cancellation, flattening, or provider outcome is implied.");
            }

            return;
        }

        if (_lastKnownConnectedStatus is { } lastConnected)
        {
            HostValue.Text = $"{lastConnected.HostId} (stale)";
            AccountValue.Text = $"{lastConnected.AccountId} (stale)";
            EnvironmentValue.Text = $"{lastConnected.Environment} (stale)";
            StateVersionValue.Text = $"{lastConnected.StateVersion} (stale)";
            LastEvidenceValue.Text = $"{lastConnected.ObservedAtUtc:O} (stale)";
            ConnectionStatus.Text =
                $"{status.Message} Last known host values are stale and are not current evidence.";
        }
        else
        {
            HostValue.Text = "Unavailable";
            AccountValue.Text = "Unavailable";
            EnvironmentValue.Text = "Unavailable";
            StateVersionValue.Text = "Unavailable";
            LastEvidenceValue.Text = "Unavailable";
            ConnectionStatus.Text = status.Message;
        }

        if (announce)
        {
            SetLiveRegionText(
                HostStatusAnnouncement,
                $"{ConnectionStatus.Text} No cancellation, flattening, or provider outcome is implied.");
        }
    }

    private static string DescribeInFlightActions(InFlightActionState value) =>
        value switch
        {
            InFlightActionState.None => "none reported",
            InFlightActionState.Present => "present",
            _ => "unknown",
        };

    private void ApplyEmergencyCommandResult(EmergencyCommandResult result)
    {
        EmergencyOperationValue.Text = result.OperationId;
        string inFlight = DescribeInFlightActions(result.InFlightActions);
        SetLiveRegionText(EmergencyResult, result switch
        {
            { Accepted: false } =>
                result.Message
                + " No durable block has been confirmed. Outstanding in-flight actions: "
                + inFlight + ".",
            { DurableBlockConfirmed: true } =>
                result.Message
                + " Durable block confirmed by the host. Outstanding in-flight actions: "
                + inFlight + ".",
            _ =>
                result.Message
                + " Request accepted, but the durable block is not yet confirmed. "
                + "Outstanding in-flight actions: " + inFlight
                + ". The same operation identity will be used for recovery; do not resubmit with a new idempotency identity.",
        });
    }

    private async Task RecoverEmergencyOperationAsync(string operationId)
    {
        try
        {
            EmergencyOperationStatus recovered =
                await _hostClient.GetOperationAsync(operationId, _lifetime.Token);

            if (!string.Equals(
                    recovered.OperationId,
                    operationId,
                    StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    "Recovered emergency operation identity does not match the accepted operation.");
            }

            EmergencyOperationValue.Text = recovered.OperationId;
            string inFlight = DescribeInFlightActions(recovered.InFlightActions);
            string suffix =
                " Outstanding in-flight actions: " + inFlight
                + ". Remaining uncertainty: " + recovered.RemainingUncertainty + ".";

            SetLiveRegionText(EmergencyResult, recovered.State switch
            {
                EmergencyOperationState.Succeeded =>
                    recovered.Message + " Durable block confirmed by the host." + suffix,
                EmergencyOperationState.Failed =>
                    recovered.Message + " The accepted operation failed; no durable block is confirmed." + suffix,
                EmergencyOperationState.Cancelled =>
                    recovered.Message + " The accepted operation was cancelled; no durable block is confirmed." + suffix,
                EmergencyOperationState.Unknown =>
                    recovered.Message
                    + " The accepted operation outcome is unknown; no durable block is confirmed."
                    + suffix
                    + " Do not resubmit with a new idempotency identity; recover this same operation.",
                _ =>
                    recovered.Message
                    + " The accepted operation is still in progress; the durable block is not yet confirmed."
                    + suffix
                    + " Recover this same operation identity rather than creating a new command.",
            });
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception)
        {
            EmergencyOperationValue.Text = operationId;
            SetLiveRegionText(
                EmergencyResult,
                "The emergency request was accepted as operation "
                + operationId
                + ", but the same operation could not be recovered. "
                + "Its durable block outcome and outstanding in-flight actions are unknown. "
                + "Do not resubmit with a new idempotency identity; recover this same operation.");
        }
    }

    private async void BlockNewExposure_Click(object sender, RoutedEventArgs e)
    {
        BlockNewExposureButton.IsEnabled = false;
        EmergencyOperationValue.Text = "Unavailable";
        SetLiveRegionText(
            EmergencyResult,
            "Requesting a durable block of new exposure from the host.");

        try
        {
            EmergencyCommandResult result =
                await _hostClient.BlockNewExposureAsync(_lifetime.Token);
            ApplyEmergencyCommandResult(result);

            if (result.Accepted && !result.DurableBlockConfirmed)
            {
                await RecoverEmergencyOperationAsync(result.OperationId);
            }

            await RefreshHostStatusAsync(announce: false, returnFocus: false);
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested)
        {
            return;
        }
        catch (EmergencyCommandUncertainException uncertain)
        {
            EmergencyOperationValue.Text = uncertain.CommandId;
            SetLiveRegionText(
                EmergencyResult,
                uncertain.Message
                + " No durable block has been confirmed. Outstanding in-flight actions are unknown. "
                + "A later Block new exposure action will recover this exact command identity rather than minting a new command.");
        }
        catch (OperationCanceledException)
        {
            EmergencyOperationValue.Text = "Unavailable";
            SetLiveRegionText(
                EmergencyResult,
                "The request was cancelled before a confirmed host result. No durable block has been confirmed. Outstanding in-flight actions are unknown.");
        }
        catch (Exception)
        {
            EmergencyOperationValue.Text = "Unavailable";
            SetLiveRegionText(
                EmergencyResult,
                "The host request failed before a confirmed result. No durable block has been confirmed. Outstanding in-flight actions are unknown.");
        }
        finally
        {
            BlockNewExposureButton.IsEnabled = true;
            if (IsLoaded)
            {
                BlockNewExposureButton.Focus();
            }
        }
    }

    private void SetWebExperienceStatus(string text)
    {
        SetLiveRegionText(WebExperienceStatus, text);
    }

    private async Task InitializeWebExperienceAsync()
    {
        DisposeWebExperience();

        if (_authenticatedHostClient is null)
        {
            SetWebExperienceStatus(
                "Embedded web experience is unavailable because no authenticated paired host session is configured. Native status and emergency controls remain available.");
            return;
        }

        WebExperienceSecurityPolicy policy =
            new(_authenticatedHostClient.BaseUri);
        WebView2? webView = null;
        try
        {
            webView = new WebView2
            {
                MinHeight = 320,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                VerticalAlignment = VerticalAlignment.Stretch,
            };
            WebExperienceContainer.Child = webView;
            _webView = webView;
            _webSecurityPolicy = policy;

            await webView.EnsureCoreWebView2Async();
            if (_lifetime.IsCancellationRequested || !IsLoaded)
            {
                return;
            }

            CoreWebView2 core = webView.CoreWebView2;
            // A prior build may have persisted an authority-bearing cookie or
            // service worker in this profile. Remove both before any trusted
            // document can navigate; current sessions are native-header only.
            core.CookieManager.DeleteAllCookies();
            await core.Profile.ClearBrowsingDataAsync(
                CoreWebView2BrowsingDataKinds.ServiceWorkers);
            if (_lifetime.IsCancellationRequested || !IsLoaded)
            {
                return;
            }

            ConfigureWebView(core, policy);
            Uri entryPoint = new(policy.HostOrigin, "/");
            _trustedTopLevelDocument = entryPoint;
            webView.Source = entryPoint;
            SetWebExperienceStatus(
                "Loading the primary web experience from the paired AutoTrade host.");
        }
        catch (Exception)
        {
            _trustedTopLevelDocument = null;
            _webSecurityPolicy = null;
            if (webView is not null)
            {
                if (ReferenceEquals(WebExperienceContainer.Child, webView))
                {
                    WebExperienceContainer.Child = null;
                }
                if (ReferenceEquals(_webView, webView))
                {
                    _webView = null;
                }
                webView.Dispose();
            }

            SetWebExperienceStatus(
                "Embedded web experience is unavailable. Native status and emergency controls remain available and do not imply any browser-side command outcome.");
        }
    }

    private void ConfigureWebView(
        CoreWebView2 core,
        WebExperienceSecurityPolicy policy)
    {
        CoreWebView2Settings settings = core.Settings;
        settings.AreDevToolsEnabled = policy.AllowsDeveloperTools;
        settings.IsWebMessageEnabled = policy.AllowsWebMessageCommandAuthority;
        settings.AreDefaultContextMenusEnabled = false;
        settings.AreDefaultScriptDialogsEnabled = false;
        settings.AreHostObjectsAllowed = false;
        settings.IsBuiltInErrorPageEnabled = false;
        settings.IsStatusBarEnabled = false;

        core.AddWebResourceRequestedFilter(
            "*",
            CoreWebView2WebResourceContext.All);
        core.WebResourceRequested += WebView_WebResourceRequested;
        core.NavigationStarting += WebView_NavigationStarting;
        core.NavigationCompleted += WebView_NavigationCompleted;
        core.FrameNavigationStarting += WebView_FrameNavigationStarting;
        core.NewWindowRequested += WebView_NewWindowRequested;
        core.DownloadStarting += WebView_DownloadStarting;
        core.PermissionRequested += WebView_PermissionRequested;
        core.ServerCertificateErrorDetected += WebView_ServerCertificateErrorDetected;
        core.ProcessFailed += WebView_ProcessFailed;
    }

    private void WebView_NavigationStarting(
        object? sender,
        CoreWebView2NavigationStartingEventArgs args)
    {
        if (_webSecurityPolicy is null
            || !Uri.TryCreate(args.Uri, UriKind.Absolute, out Uri? target)
            || !_webSecurityPolicy.AllowsTopLevelNavigation(target))
        {
            args.Cancel = true;
            _trustedTopLevelDocument = null;
            SetWebExperienceStatus(
                "Blocked an untrusted embedded navigation. Native status and emergency controls remain available.");
            return;
        }

        _trustedTopLevelDocument = target;
    }

    private void WebView_NavigationCompleted(
        object? sender,
        CoreWebView2NavigationCompletedEventArgs args)
    {
        if (!args.IsSuccess)
        {
            _trustedTopLevelDocument = null;
            SetWebExperienceStatus(
                "Embedded web navigation failed. Native status and emergency controls remain available.");
            return;
        }

        SetWebExperienceStatus(
            "Primary web experience loaded from the paired AutoTrade host. Financial state and command outcomes remain authoritative in the host.");
    }

    private void WebView_WebResourceRequested(
        object? sender,
        CoreWebView2WebResourceRequestedEventArgs args)
    {
        CoreWebView2WebResourceRequest request = args.Request;

        // Browser/JS supplied credential material is never trusted. Native code
        // removes it first, then selectively attaches the current paired session
        // only to exact Host API operations admitted by the canonical policy.
        request.Headers.RemoveHeader("Authorization");
        request.Headers.RemoveHeader("X-AutoTrade-Actor");

        if (_authenticatedHostClient is null
            || _webSecurityPolicy is null
            || _trustedTopLevelDocument is null
            || args.RequestedSourceKind != CoreWebView2WebResourceRequestSourceKinds.Document
            || !IsSessionForwardingResourceContext(args.ResourceContext)
            || !Uri.TryCreate(request.Uri, UriKind.Absolute, out Uri? target)
            || !_webSecurityPolicy.AllowsSessionHeaderForwarding(
                request.Method,
                target,
                _trustedTopLevelDocument))
        {
            return;
        }

        try
        {
            EmergencyHostSession session =
                _authenticatedHostClient.GetBoundSessionForEmbeddedWeb();
            request.Headers.SetHeader(
                "Authorization",
                "AutoTrade-Session " + session.Token);
            request.Headers.SetHeader(
                "X-AutoTrade-Actor",
                session.Actor);
        }
        catch (Exception)
        {
            // Missing/expired/mismatched credentials fail closed: the request is
            // sent without credentials and the host remains the rejecting authority.
        }
    }

    private static bool IsSessionForwardingResourceContext(
        CoreWebView2WebResourceContext context) =>
        context is CoreWebView2WebResourceContext.Fetch
            or CoreWebView2WebResourceContext.XmlHttpRequest;

    private void WebView_FrameNavigationStarting(
        object? sender,
        CoreWebView2NavigationStartingEventArgs args)
    {
        args.Cancel = true;
        SetWebExperienceStatus(
            "Blocked an embedded frame navigation. The trusted product UI has no frame workflow.");
    }

    private void WebView_NewWindowRequested(
        object? sender,
        CoreWebView2NewWindowRequestedEventArgs args)
    {
        args.Handled = true;
        SetWebExperienceStatus(
            "Blocked an embedded new-window request. Native controls remain available.");
    }

    private void WebView_DownloadStarting(
        object? sender,
        CoreWebView2DownloadStartingEventArgs args)
    {
        args.Cancel = true;
        SetWebExperienceStatus(
            "Blocked an embedded download request. Downloads are not part of the trusted financial UI.");
    }

    private void WebView_PermissionRequested(
        object? sender,
        CoreWebView2PermissionRequestedEventArgs args)
    {
        args.State = CoreWebView2PermissionState.Deny;
    }

    private void WebView_ServerCertificateErrorDetected(
        object? sender,
        CoreWebView2ServerCertificateErrorDetectedEventArgs args)
    {
        args.Action = CoreWebView2ServerCertificateErrorAction.Cancel;
        _trustedTopLevelDocument = null;
        SetWebExperienceStatus(
            "Blocked a certificate-invalid web request. No browser content is trusted until the web experience is reloaded.");
    }

    private void WebView_ProcessFailed(
        object? sender,
        CoreWebView2ProcessFailedEventArgs args)
    {
        _trustedTopLevelDocument = null;
        SetWebExperienceStatus(
            "Embedded web process failed (" + args.ProcessFailedKind
            + "). No browser content is trusted after the failure. "
            + "Native status and emergency controls remain available. "
            + "Use Reload web experience to create a fresh browser surface.");
    }

    private void DisposeWebExperience()
    {
        _trustedTopLevelDocument = null;
        _webSecurityPolicy = null;
        WebView2? webView = _webView;
        _webView = null;
        if (webView is null)
        {
            WebExperienceContainer.Child = null;
            return;
        }

        if (ReferenceEquals(WebExperienceContainer.Child, webView))
        {
            WebExperienceContainer.Child = null;
        }
        webView.Dispose();
    }

    private async void ReloadWebExperience_Click(object sender, RoutedEventArgs e)
    {
        ReloadWebExperienceButton.IsEnabled = false;
        try
        {
            await InitializeWebExperienceAsync();
        }
        finally
        {
            ReloadWebExperienceButton.IsEnabled = true;
            if (IsLoaded)
            {
                ReloadWebExperienceButton.Focus();
            }
        }
    }

}
