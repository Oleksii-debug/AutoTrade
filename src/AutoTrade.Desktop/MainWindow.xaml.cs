using System.Windows;

namespace AutoTrade.Desktop;

public partial class MainWindow : Window
{
    private readonly IEmergencyHostClient _hostClient;

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

    private async void BlockNewExposure_Click(object sender, RoutedEventArgs e)
    {
        BlockNewExposureButton.IsEnabled = false;
        EmergencyResult.Text = "Requesting a durable block of new exposure from the host.";

        try
        {
            EmergencyCommandResult result =
                await _hostClient.BlockNewExposureAsync(CancellationToken.None);
            EmergencyResult.Text = result.Accepted
                ? result.Message + " The request was accepted; provider and financial outcomes remain separately tracked."
                : result.Message;
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
            BlockNewExposureButton.Focus();
        }
    }
}
