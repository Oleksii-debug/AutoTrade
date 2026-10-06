using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Threading;
using System.IO;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace AutoTrade.Desktop;

public partial class MainWindow : Window
{
    private readonly IEmergencyHostClient _hostClient;
    private readonly IEmergencyHostSessionProvider? _sessionProvider;
    private readonly CancellationTokenSource _lifetime = new();
    private readonly DispatcherTimer _hostRefreshTimer = new()
    {
        Interval = TimeSpan.FromSeconds(10),
    };
    private bool _hostRefreshInProgress;
    private bool _announceHostRefreshCompletion;
    private HostDisplayFreshness? _lastDisplayedFreshness;
    private EmergencyHostStatus? _lastKnownConnectedStatus;
    private EmergencyHostStatus? _lastKnownCurrentStatus;
    private WebView2? _productWebView;
    private long _webGeneration;
    private bool _trustedWebDocumentActive;

    private enum HostDisplayFreshness
    {
        Current,
        Stale,
        Busy,
        Disconnected,
    }

    public MainWindow()
        : this(DesktopHostClientFactory.CreateConnection())
    {
    }

    private MainWindow(DesktopHostConnection connection)
        : this(
            (connection ?? throw new ArgumentNullException(nameof(connection))).Client,
            connection.SessionProvider)
    {
    }

    internal MainWindow(
        IEmergencyHostClient hostClient,
        IEmergencyHostSessionProvider? sessionProvider = null)
    {
        _hostClient = hostClient ?? throw new ArgumentNullException(nameof(hostClient));
        _sessionProvider = sessionProvider;
        InitializeComponent();
        _hostRefreshTimer.Tick += HostRefreshTimer_Tick;
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
        // Native host truth is safety-critical and must not wait for optional
        // WebView2 runtime/profile initialization.
        await RefreshHostStatusAsync(announce: true, returnFocus: false);
        if (!_lifetime.IsCancellationRequested)
        {
            _hostRefreshTimer.Start();
            await ConnectWebExperienceAsync();
        }
    }

    private async Task ConnectWebExperienceAsync()
    {
        DisposeWebExperience();
        FocusWebButton.IsEnabled = false;
        ReloadWebButton.IsEnabled = false;
        ProductWebViewHost.Visibility = Visibility.Collapsed;

        if (_sessionProvider is null)
        {
            SetLiveRegionText(
                WebExperienceStatus,
                "Web interface unavailable because no paired host session is configured. Native host status and emergency controls remain available.");
            return;
        }

        WebView2? webView = null;
        try
        {
            EmergencyHostSession initialSession = _sessionProvider.GetSession().Validated();
            Uri origin = initialSession.Origin;
            WebExperienceSecurityPolicy policy = new(origin);
            string userDataFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "AutoTrade",
                "WebView2");
            CoreWebView2Environment environment =
                await CoreWebView2Environment.CreateAsync(userDataFolder: userDataFolder);
            if (_lifetime.IsCancellationRequested || !IsLoaded)
            {
                return;
            }

            webView = new WebView2
            {
                Height = 600,
                Visibility = Visibility.Collapsed,
            };
            AutomationProperties.SetName(
                webView,
                "AutoTrade application web interface");
            ProductWebViewHost.Child = webView;
            _productWebView = webView;
            long generation = ++_webGeneration;

            await webView.EnsureCoreWebView2Async(environment);
            if (_lifetime.IsCancellationRequested || !IsLoaded)
            {
                DisposeWebExperience();
                return;
            }

            CoreWebView2 core = webView.CoreWebView2;
            core.Settings.AreDevToolsEnabled = policy.AllowsDeveloperTools;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreDefaultScriptDialogsEnabled = false;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsBuiltInErrorPageEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsWebMessageEnabled = policy.AllowsWebMessageCommandAuthority;
            core.NavigationStarting += (_, e) =>
            {
                if (!IsCurrentWebGeneration(webView, generation))
                {
                    e.Cancel = true;
                    return;
                }

                bool admitted =
                    Uri.TryCreate(e.Uri, UriKind.Absolute, out Uri? target)
                    && policy.AllowsTopLevelNavigation(target);
                e.Cancel = !admitted;
                _trustedWebDocumentActive = admitted;
                if (!admitted)
                {
                    SetLiveRegionText(
                        WebExperienceStatus,
                        "Blocked an untrusted web navigation. Native host status and emergency controls remain available.");
                }
            };
            core.NavigationCompleted += (_, e) =>
            {
                if (!IsCurrentWebGeneration(webView, generation))
                {
                    return;
                }

                if (!e.IsSuccess)
                {
                    _trustedWebDocumentActive = false;
                    ReloadWebButton.IsEnabled = true;
                    SetLiveRegionText(
                        WebExperienceStatus,
                        "Web navigation failed. No browser content is trusted. Native host status and emergency controls remain available.");
                    return;
                }

                SetLiveRegionText(
                    WebExperienceStatus,
                    "AutoTrade web interface connected to the paired host. Native host status and emergency controls remain independently available.");
            };
            core.FrameNavigationStarting += (_, e) => e.Cancel = true;
            core.NewWindowRequested += (_, e) => e.Handled = true;
            core.DownloadStarting += (_, e) => e.Cancel = true;
            core.PermissionRequested += (_, e) =>
                e.State = CoreWebView2PermissionState.Deny;
            core.ServerCertificateErrorDetected += (_, e) =>
            {
                e.Action = CoreWebView2ServerCertificateErrorAction.Cancel;
                if (!IsCurrentWebGeneration(webView, generation))
                {
                    return;
                }

                _trustedWebDocumentActive = false;
                ReloadWebButton.IsEnabled = true;
                SetLiveRegionText(
                    WebExperienceStatus,
                    "Blocked a certificate-invalid web request. No browser content is trusted until the web interface is reloaded.");
            };
            core.ProcessFailed += (_, e) =>
                WebView_ProcessFailed(webView, generation, e);

            // Never persist the host credential in WebView state. Purge stale
            // cookies/service workers before first trusted navigation.
            core.CookieManager.DeleteAllCookies();
            await core.Profile.ClearBrowsingDataAsync(
                CoreWebView2BrowsingDataKinds.ServiceWorkers);
            if (_lifetime.IsCancellationRequested || !IsLoaded)
            {
                DisposeWebExperience();
                return;
            }

            core.AddWebResourceRequestedFilter(
                new Uri(origin, "api/v1/*").AbsoluteUri,
                CoreWebView2WebResourceContext.All,
                CoreWebView2WebResourceRequestSourceKinds.Document);
            core.WebResourceRequested += (_, e) =>
            {
                void StripAuthority()
                {
                    e.Request.Headers.RemoveHeader("Authorization");
                    e.Request.Headers.RemoveHeader("X-AutoTrade-Actor");
                }

                // Strip caller/browser authority first. Native authority is then
                // attached only to a scripted request from the admitted top-level document.
                StripAuthority();
                if (!IsCurrentWebGeneration(webView, generation))
                {
                    return;
                }

                try
                {
                    EmergencyHostSession currentSession =
                        _sessionProvider.GetSession().Validated();
                    bool sameOrigin =
                        Uri.Compare(
                            currentSession.Origin,
                            origin,
                            UriComponents.SchemeAndServer,
                            UriFormat.UriEscaped,
                            StringComparison.OrdinalIgnoreCase) == 0;
                    bool scriptedApiRequest =
                        e.ResourceContext is CoreWebView2WebResourceContext.Fetch
                            or CoreWebView2WebResourceContext.XmlHttpRequest;
                    bool admitted =
                        _trustedWebDocumentActive
                        && sameOrigin
                        && scriptedApiRequest
                        && e.RequestedSourceKind
                            == CoreWebView2WebResourceRequestSourceKinds.Document
                        && Uri.TryCreate(
                            e.Request.Uri,
                            UriKind.Absolute,
                            out Uri? target)
                        && policy.AllowsSessionHeaderForwarding(
                            e.Request.Method,
                            target,
                            origin);
                    if (!admitted)
                    {
                        return;
                    }

                    e.Request.Headers.SetHeader(
                        "Authorization",
                        "AutoTrade-Session " + currentSession.Token);
                    e.Request.Headers.SetHeader(
                        "X-AutoTrade-Actor",
                        currentSession.Actor);
                }
                catch (Exception)
                {
                    StripAuthority();
                }
            };

            core.Navigate(origin.AbsoluteUri);
            webView.Visibility = Visibility.Visible;
            ProductWebViewHost.Visibility = Visibility.Visible;
            FocusWebButton.IsEnabled = true;
            ReloadWebButton.IsEnabled = true;
            SetLiveRegionText(
                WebExperienceStatus,
                "Loading the AutoTrade web interface from the paired host.");
        }
        catch (Exception)
        {
            DisposeWebExperience();
            FocusWebButton.IsEnabled = false;
            ReloadWebButton.IsEnabled = _sessionProvider is not null;
            SetLiveRegionText(
                WebExperienceStatus,
                "Web interface unavailable. Check the paired host session and installed WebView2 Runtime. Native host status and emergency controls remain available.");
        }
    }

    private bool IsCurrentWebGeneration(WebView2 webView, long generation) =>
        generation == _webGeneration
        && ReferenceEquals(webView, _productWebView);

    private static bool RequiresFreshWebViewAfterFailure(
        CoreWebView2ProcessFailedKind kind) =>
        kind is CoreWebView2ProcessFailedKind.BrowserProcessExited
            or CoreWebView2ProcessFailedKind.RenderProcessExited
            or CoreWebView2ProcessFailedKind.RenderProcessUnresponsive;

    private void WebView_ProcessFailed(
        WebView2 webView,
        long generation,
        CoreWebView2ProcessFailedEventArgs e)
    {
        if (!IsCurrentWebGeneration(webView, generation)
            || !RequiresFreshWebViewAfterFailure(e.ProcessFailedKind))
        {
            return;
        }

        // Revoke browser trust immediately. Recovery is user-triggered after the
        // event returns so BrowserProcessExited can be repaired with a fresh
        // WebView2 instance without re-entering the failing callback.
        _trustedWebDocumentActive = false;
        FocusWebButton.IsEnabled = false;
        ProductWebViewHost.Visibility = Visibility.Collapsed;
        ReloadWebButton.IsEnabled = true;
        SetLiveRegionText(
            WebExperienceStatus,
            "The WebView2 process failed (" + e.ProcessFailedKind
            + "). No browser content is trusted. Native host status and emergency controls remain available. Use Reload application web interface to recreate the browser surface.");
    }

    private void DisposeWebExperience()
    {
        _trustedWebDocumentActive = false;
        _webGeneration++;
        WebView2? webView = _productWebView;
        _productWebView = null;
        ProductWebViewHost.Child = null;
        webView?.Dispose();
    }

    private void FocusWeb_Click(object sender, RoutedEventArgs e) =>
        _productWebView?.Focus();

    private async void ReloadWeb_Click(object sender, RoutedEventArgs e)
    {
        ReloadWebButton.IsEnabled = false;
        await ConnectWebExperienceAsync();
        if (IsLoaded)
        {
            ReloadWebButton.Focus();
        }
    }

    private void MainWindow_Closed(object? sender, EventArgs e)
    {
        _hostRefreshTimer.Stop();
        _lifetime.Cancel();
        DisposeWebExperience();
    }

    private async void HostRefreshTimer_Tick(object? sender, EventArgs e)
    {
        await RefreshHostStatusAsync(announce: false, returnFocus: false);
    }

    private async void RefreshHostStatus_Click(object sender, RoutedEventArgs e)
    {
        await RefreshHostStatusAsync(announce: true, returnFocus: true);
    }

    private async Task RefreshHostStatusAsync(bool announce, bool returnFocus)
    {
        if (_lifetime.IsCancellationRequested)
        {
            return;
        }

        if (_hostRefreshInProgress)
        {
            if (announce)
            {
                _announceHostRefreshCompletion = true;
                SetLiveRegionText(
                    HostStatusAnnouncement,
                    "Host status refresh is already in progress. The current request will update this status when it finishes.");
            }
            return;
        }

        _hostRefreshInProgress = true;
        _announceHostRefreshCompletion = false;
        bool manageRefreshButton = returnFocus;
        if (manageRefreshButton)
        {
            RefreshStatusButton.IsEnabled = false;
        }

        try
        {
            EmergencyHostStatus status = await _hostClient.GetStatusAsync(_lifetime.Token);
            ApplyHostStatus(
                status,
                announce || _announceHostRefreshCompletion);
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested)
        {
            return;
        }
        catch (EmergencySnapshotBusyException)
        {
            ApplySnapshotBusyStatus(
                announce || _announceHostRefreshCompletion);
        }
        catch (Exception)
        {
            ApplyHostStatus(
                EmergencyHostStatus.Disconnected(
                    "Host refresh failed. No new host evidence was accepted."),
                announce || _announceHostRefreshCompletion);
        }
        finally
        {
            if (manageRefreshButton)
            {
                RefreshStatusButton.IsEnabled = true;
            }

            _announceHostRefreshCompletion = false;
            _hostRefreshInProgress = false;
            if (returnFocus && IsLoaded)
            {
                RefreshStatusButton.Focus();
            }
        }
    }

    private void ApplySnapshotBusyStatus(bool announce)
    {
        const string message =
            "Host snapshot is temporarily busy while durable state changes. "
            + "No new host evidence was accepted. Retry is safe.";
        bool announceTransition =
            _lastDisplayedFreshness is { } previousFreshness
            && previousFreshness != HostDisplayFreshness.Busy;
        _lastDisplayedFreshness = HostDisplayFreshness.Busy;

        if (_lastKnownConnectedStatus is { } lastConnected)
        {
            HostValue.Text = $"{lastConnected.HostId} (stale)";
            AccountValue.Text = $"{lastConnected.AccountId} (stale)";
            EnvironmentValue.Text = $"{lastConnected.Environment} (stale)";
            StateVersionValue.Text = $"{lastConnected.StateVersion} (stale)";
            LastEvidenceValue.Text = $"{lastConnected.ObservedAtUtc:O} (stale)";
            ConnectionStatus.Text =
                message
                + " Last known host values are stale and are not current evidence.";
        }
        else
        {
            HostValue.Text = "Unavailable";
            AccountValue.Text = "Unavailable";
            EnvironmentValue.Text = "Unavailable";
            StateVersionValue.Text = "Unavailable";
            LastEvidenceValue.Text = "Unavailable";
            ConnectionStatus.Text =
                message + " No verified host snapshot is currently available.";
        }

        if (announce || announceTransition)
        {
            SetLiveRegionText(
                HostStatusAnnouncement,
                $"{ConnectionStatus.Text} No cancellation, flattening, provider outcome, or command acceptance is implied.");
        }
    }

    private void ApplyHostStatus(EmergencyHostStatus status, bool announce)
    {
        status = (status ?? throw new InvalidOperationException(
            "Host status response was null.")).Validated();

        HostDisplayFreshness freshness = !status.Connected
            ? HostDisplayFreshness.Disconnected
            : status.IsCurrent
                ? HostDisplayFreshness.Current
                : HostDisplayFreshness.Stale;
        bool announceTransition =
            _lastDisplayedFreshness is { } previousFreshness
            && previousFreshness != freshness;

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

            _lastDisplayedFreshness = freshness;

            if (announce || announceTransition)
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

        _lastDisplayedFreshness = freshness;

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

        if (announce || announceTransition)
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
}
