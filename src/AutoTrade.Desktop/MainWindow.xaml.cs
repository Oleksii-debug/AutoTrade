using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Threading;

namespace AutoTrade.Desktop;

public partial class MainWindow : Window
{
    private readonly IEmergencyHostClient _hostClient;
    private readonly CancellationTokenSource _lifetime = new();
    private readonly DispatcherTimer _hostRefreshTimer = new()
    {
        Interval = TimeSpan.FromSeconds(10),
    };
    private bool _hostRefreshInProgress;
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
    {
        _hostClient = hostClient ?? throw new ArgumentNullException(nameof(hostClient));
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
        await RefreshHostStatusAsync(announce: true, returnFocus: false);
        if (!_lifetime.IsCancellationRequested)
        {
            _hostRefreshTimer.Start();
        }
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
        if (_hostRefreshInProgress || _lifetime.IsCancellationRequested)
        {
            return;
        }

        _hostRefreshInProgress = true;
        // Only an explicit keyboard/button invocation owns the button state.
        // Periodic refresh must not disable a focusable control every ten
        // seconds, because doing so can evict keyboard/NVDA focus.
        bool manageRefreshButton = returnFocus;
        if (manageRefreshButton)
        {
            RefreshStatusButton.IsEnabled = false;
        }

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
            if (manageRefreshButton)
            {
                RefreshStatusButton.IsEnabled = true;
            }
            _hostRefreshInProgress = false;
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
