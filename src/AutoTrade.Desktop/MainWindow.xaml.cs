using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Threading;
using System.IO;
using Microsoft.Web.WebView2.Core;

namespace AutoTrade.Desktop;

public partial class MainWindow : Window
{
    private readonly IEmergencyHostClient _hostClient;
    private readonly CancellationTokenSource _lifetime = new();
    private readonly DispatcherTimer _hostRefreshTimer = new()
    {
        Interval = TimeSpan.FromSeconds(10),
    };
    private readonly OwnedProviderFreeRuntime? _ownedRuntime;
    private bool _closing;
    private bool _stopComplete;
    private bool _hostRefreshInProgress;
    private bool _announceHostRefreshCompletion;
    private HostDisplayFreshness? _lastDisplayedFreshness;
    private EmergencyHostStatus? _lastKnownConnectedStatus;
    private EmergencyHostStatus? _lastKnownCurrentStatus;

    private enum HostDisplayFreshness
    {
        Current,
        Stale,
        Disconnected,
    }

    public MainWindow()
        : this(DesktopHostClientFactory.Create())
    {
    }

    internal MainWindow(IEmergencyHostClient hostClient)
        : this(hostClient, null)
    {
    }

    internal MainWindow(IEmergencyHostClient hostClient, OwnedProviderFreeRuntime? ownedRuntime)
    {
        _hostClient = hostClient ?? throw new ArgumentNullException(nameof(hostClient));
        _ownedRuntime = ownedRuntime;
        InitializeComponent();
        _hostRefreshTimer.Tick += HostRefreshTimer_Tick;
        Closing += MainWindow_Closing;
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

    private void SetLiveRegionText(TextBox element, string text)
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
        await ConnectWebExperienceAsync();
        await RefreshHostStatusAsync(announce: true, returnFocus: false);
        if (!_lifetime.IsCancellationRequested)
        {
            _hostRefreshTimer.Start();
        }
    }

    private async Task ConnectWebExperienceAsync()
    {
        if (_ownedRuntime is null) return;
        try
        {
            WebExperienceSecurityPolicy policy = new(_ownedRuntime.Origin);
            CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(
                userDataFolder: Path.Combine(_ownedRuntime.DataDirectory, "webview2"));
            await ProductWebView.EnsureCoreWebView2Async(environment);
            CoreWebView2 core = ProductWebView.CoreWebView2;
            core.Settings.AreDevToolsEnabled = policy.AllowsDeveloperTools;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsWebMessageEnabled = false;
            core.NavigationStarting += (_, e) =>
            {
                e.Cancel = !Uri.TryCreate(e.Uri, UriKind.Absolute, out Uri? target)
                    || !policy.AllowsTopLevelNavigation(target);
            };
            core.NewWindowRequested += (_, e) => e.Handled = true;
            core.DownloadStarting += (_, e) => e.Cancel = true;
            core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.ServerCertificateErrorDetected += (_, e) => e.Action = CoreWebView2ServerCertificateErrorAction.Cancel;
            CoreWebView2Cookie cookie = core.CookieManager.CreateCookie("AutoTradeSession",
                _ownedRuntime.CookieToken, "127.0.0.1", "/api/v1");
            cookie.IsHttpOnly = true;
            cookie.SameSite = CoreWebView2CookieSameSiteKind.Strict;
            core.CookieManager.AddOrUpdateCookie(cookie);
            core.Navigate(_ownedRuntime.Origin.AbsoluteUri);
            ProductWebView.Visibility = Visibility.Visible;
            FocusWebButton.IsEnabled = true;
            SetLiveRegionText(WebExperienceStatus,
                "Provider-free web interface connected. Use Tab or Focus application web interface to enter it. Real orders are unavailable.");
        }
        catch (Exception)
        {
            SetLiveRegionText(WebExperienceStatus,
                "Web interface unavailable. Check the installed WebView2 Runtime. Native host status and emergency controls remain available.");
        }
    }

    private void FocusWeb_Click(object sender, RoutedEventArgs e) => ProductWebView.Focus();

    private async void MainWindow_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_ownedRuntime is null || _stopComplete) return;
        e.Cancel = true;
        if (_closing) return;
        _closing = true;
        FocusWebButton.IsEnabled = false;
        SetLiveRegionText(WebExperienceStatus, "Stopping the local host and draining accepted work.");
        try { await _ownedRuntime.DisposeAsync(); }
        catch (Exception) { SetLiveRegionText(WebExperienceStatus, "Host stop failed. State requires recovery at the next launch."); }
        ProductWebView.Dispose();
        _stopComplete = true;
        Close();
    }

    private void MainWindow_Closed(object? sender, EventArgs e)
    {
        _hostRefreshTimer.Stop();
        _lifetime.Cancel();
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
        bool restoreKeyboardFocus =
            manageRefreshButton && RefreshStatusButton.IsKeyboardFocusWithin;
        if (manageRefreshButton)
        {
            RefreshStatusButton.IsEnabled = false;
        }

        try
        {
            EmergencyHostStatus status = await _hostClient.GetStatusAsync(_lifetime.Token);
            ApplyHostStatus(status, announce || _announceHostRefreshCompletion);
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
            var focusedElement =
                System.Windows.Input.Keyboard.FocusedElement;
            if (manageRefreshButton)
            {
                RefreshStatusButton.IsEnabled = true;
            }
            _announceHostRefreshCompletion = false;
            _hostRefreshInProgress = false;
            if (restoreKeyboardFocus && IsLoaded && (
                focusedElement is null
                || ReferenceEquals(focusedElement, RefreshStatusButton)
                || ReferenceEquals(focusedElement, this)))
            {
                RefreshStatusButton.Focus();
            }
        }
    }

    private void ApplySnapshotBusyStatus(bool announce)
    {
        bool announceTransition =
            _lastDisplayedFreshness != HostDisplayFreshness.Stale;
        const string message =
            "Host is responding, but one coherent state snapshot is temporarily busy. "
            + "No new host evidence was accepted.";

        if (_lastKnownConnectedStatus is { } lastConnected)
        {
            HostValue.Text = $"{lastConnected.HostId} (stale)";
            AccountValue.Text = $"{lastConnected.AccountId} (stale)";
            EnvironmentValue.Text = $"{lastConnected.Environment} (stale)";
            StateVersionValue.Text = $"{lastConnected.StateVersion} (stale)";
            LastEvidenceValue.Text =
                $"{lastConnected.ObservedAtUtc:O} (stale)";
            ConnectionStatus.Text =
                message
                + " Last known host values remain visible only as stale evidence.";
        }
        else
        {
            HostValue.Text = "Unavailable";
            AccountValue.Text = "Unavailable";
            EnvironmentValue.Text = "Unavailable";
            StateVersionValue.Text = "Unavailable";
            LastEvidenceValue.Text = "Unavailable";
            ConnectionStatus.Text =
                message
                + " No canonical host snapshot has been accepted yet.";
        }

        _lastDisplayedFreshness = HostDisplayFreshness.Stale;
        if (announce || announceTransition)
        {
            SetLiveRegionText(
                HostStatusAnnouncement,
                $"{ConnectionStatus.Text} Retry is safe. "
                + "No cancellation, flattening, or provider outcome is implied.");
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
        bool announceTransition = _lastDisplayedFreshness is { } previousFreshness
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

        _lastDisplayedFreshness = freshness;

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
        bool restoreKeyboardFocus = BlockNewExposureButton.IsKeyboardFocusWithin;
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
        catch (EmergencySnapshotBusyException)
        {
            EmergencyOperationValue.Text = "Unavailable";
            SetLiveRegionText(
                EmergencyResult,
                "The host is responding, but one coherent state snapshot is temporarily busy. "
                + "No emergency command was created or sent, and no durable block has been confirmed. "
                + "Retry the same Block new exposure action after the snapshot becomes available.");
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
            System.Windows.IInputElement? focusedElement =
                System.Windows.Input.Keyboard.FocusedElement;
            BlockNewExposureButton.IsEnabled = true;
            if (restoreKeyboardFocus && IsLoaded && (
                focusedElement is null
                || ReferenceEquals(focusedElement, BlockNewExposureButton)
                || ReferenceEquals(focusedElement, this)))
            {
                BlockNewExposureButton.Focus();
            }
        }
    }
}
