using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Controls;

namespace AutoTrade.Desktop;

public partial class MainWindow : Window
{
    private readonly IEmergencyHostClient _hostClient;
    private readonly CancellationTokenSource _lifetime = new();
    private EmergencyHostStatus? _lastKnownConnectedStatus;

    public MainWindow()
        : this(DesktopHostClientFactory.Create())
    {
    }

    internal MainWindow(IEmergencyHostClient hostClient)
    {
        _hostClient = hostClient ?? throw new ArgumentNullException(nameof(hostClient));
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
    }

    private void MainWindow_Closed(object? sender, EventArgs e)
    {
        _lifetime.Cancel();
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

            if (status.IsCurrent)
            {
                _lastKnownConnectedStatus = status;
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
}
