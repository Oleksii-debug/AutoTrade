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
        if (status.Connected)
        {
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

        if (_lastKnownConnectedStatus is { } previous)
        {
            HostValue.Text = $"{previous.HostId} (stale)";
            AccountValue.Text = $"{previous.AccountId} (stale)";
            EnvironmentValue.Text = $"{previous.Environment} (stale)";
            StateVersionValue.Text = $"{previous.StateVersion} (stale)";
            LastEvidenceValue.Text = $"{previous.ObservedAtUtc:O} (stale)";
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
        EmergencyResult.Text = "Requesting a durable block of new exposure from the host.";

        try
        {
            EmergencyCommandResult result =
                await _hostClient.BlockNewExposureAsync(_lifetime.Token);
            EmergencyResult.Text = result.Accepted
                ? result.Message + " The request was accepted; provider and financial outcomes remain separately tracked."
                : result.Message;

            await RefreshHostStatusAsync(announce: false, returnFocus: false);
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested)
        {
            return;
        }
        catch (OperationCanceledException)
        {
            EmergencyResult.Text = "The request was cancelled. No durable block has been confirmed.";
        }
        catch (Exception)
        {
            EmergencyResult.Text = "The host request failed. No durable block has been confirmed.";
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
