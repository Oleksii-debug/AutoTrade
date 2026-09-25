using System.Windows;

namespace AutoTrade.Desktop;

public partial class MainWindow : Window
{
    private readonly IEmergencyHostClient _hostClient;
    private readonly CancellationTokenSource _lifetime = new();
    private EmergencyHostStatus? _lastKnownConnectedStatus;

    public MainWindow()
        : this(new DisconnectedEmergencyHostClient())
    {
    }

    internal MainWindow(IEmergencyHostClient hostClient)
    {
        _hostClient = hostClient ?? throw new ArgumentNullException(nameof(hostClient));
        InitializeComponent();
        ConnectionStatus.Text = "Host unavailable; new exposure cannot be confirmed blocked from this window.";
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
                status = status.ValidateSuccessorOf(previous);
            }

            _lastKnownConnectedStatus = status;
            HostValue.Text = status.HostId;
            AccountValue.Text = status.AccountId;
            EnvironmentValue.Text = status.Environment;
            StateVersionValue.Text = status.StateVersion;
            LastEvidenceValue.Text = status.ObservedAtUtc.ToString("O");
            ConnectionStatus.Text = status.Message;
            if (announce)
            {
                HostStatusAnnouncement.Text =
                    $"Host status refreshed. {status.Message} State version {status.StateVersion}.";
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
            HostStatusAnnouncement.Text =
                $"{ConnectionStatus.Text} No cancellation, flattening, or provider outcome is implied.";
        }
    }

    private async void BlockNewExposure_Click(object sender, RoutedEventArgs e)
    {
        BlockNewExposureButton.IsEnabled = false;
        EmergencyOperationValue.Text = "Unavailable";
        EmergencyResult.Text = "Requesting a durable block of new exposure from the host.";

        try
        {
            EmergencyCommandResult result =
                await _hostClient.BlockNewExposureAsync(_lifetime.Token);
            EmergencyOperationValue.Text = result.OperationId;
            string inFlight = result.InFlightActions switch
            {
                InFlightActionState.None => "none reported",
                InFlightActionState.Present => "present",
                _ => "unknown",
            };
            EmergencyResult.Text = result switch
            {
                { Accepted: false } =>
                    result.Message + " No durable block has been confirmed. Outstanding in-flight actions: " + inFlight + ".",
                { DurableBlockConfirmed: true } =>
                    result.Message + " Durable block confirmed by the host. Outstanding in-flight actions: " + inFlight + ".",
                _ =>
                    result.Message + " Request accepted, but the durable block is not yet confirmed. Outstanding in-flight actions: " + inFlight + ".",
            };

            await RefreshHostStatusAsync(announce: false, returnFocus: false);
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested)
        {
            return;
        }
        catch (OperationCanceledException)
        {
            EmergencyOperationValue.Text = "Unavailable";
            EmergencyResult.Text = "The request was cancelled. No durable block has been confirmed. Outstanding in-flight actions are unknown.";
        }
        catch (Exception)
        {
            EmergencyOperationValue.Text = "Unavailable";
            EmergencyResult.Text = "The host request failed. No durable block has been confirmed. Outstanding in-flight actions are unknown.";
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
