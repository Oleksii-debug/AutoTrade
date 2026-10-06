using System.Windows;

namespace AutoTrade.Desktop;

public partial class App : Application
{
    private static bool ExternalHostConfigurationRequested(
        string? hostUri,
        string? credentialTarget) =>
        hostUri is not null || credentialTarget is not null;

    [STAThread]
    private static void Main(string[] args)
    {
        App app = new();
        app.InitializeComponent();
        app.Run();
    }

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        MainWindow window;
        try
        {
            string? hostUri = Environment.GetEnvironmentVariable("AUTOTRADE_HOST_URI");
            string? credentialTarget =
                Environment.GetEnvironmentVariable("AUTOTRADE_HOST_CREDENTIAL_TARGET");
            bool externalConfigurationRequested =
                ExternalHostConfigurationRequested(hostUri, credentialTarget);

            if (externalConfigurationRequested)
            {
                DesktopHostConnection connection =
                    DesktopHostClientFactory.CreateConnection();
                window = new(connection.Client, connection.SessionProvider);
            }
            else
            {
                OwnedProviderFreeRuntime owned =
                    await OwnedProviderFreeRuntime.StartAsync();
                window = new(owned.Client, owned, owned);
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(
                error.Message,
                "AutoTrade startup failed",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            Shutdown(2);
            return;
        }

        MainWindow = window;
        window.Show();
    }
}
