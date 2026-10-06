using System.Windows;

namespace AutoTrade.Desktop;

public partial class App : Application
{
    private static bool ExternalHostConfigurationRequested(
        string? hostUri,
        string? credentialTarget) =>
        hostUri is not null || credentialTarget is not null;

    private static bool RunOwnedRuntimeSmokeIfRequested(string[] args)
    {
        if (args.Length != 2
            || !string.Equals(
                args[0],
                "--owned-runtime-smoke",
                StringComparison.Ordinal))
        {
            return false;
        }

        OwnedProviderFreeRuntime owned =
            OwnedProviderFreeRuntime.StartAsync(
                AppContext.BaseDirectory,
                args[1]).GetAwaiter().GetResult();
        try
        {
            Uri origin = owned.Origin;
            if (origin.Scheme != Uri.UriSchemeHttp
                || origin.Host != "127.0.0.1"
                || origin.Port <= 0
                || origin.AbsolutePath != "/"
                || !string.IsNullOrEmpty(origin.UserInfo)
                || !string.IsNullOrEmpty(origin.Query)
                || !string.IsNullOrEmpty(origin.Fragment))
            {
                throw new InvalidOperationException(
                    "Owned runtime smoke did not bind a canonical loopback origin.");
            }
            if (!string.Equals(
                    Path.GetFullPath(owned.DataDirectory),
                    Path.GetFullPath(args[1]),
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(
                    "Owned runtime smoke escaped the requested isolated data directory.");
            }
        }
        finally
        {
            owned.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
        return true;
    }

    [STAThread]
    private static void Main(string[] args)
    {
        if (RunOwnedRuntimeSmokeIfRequested(args))
        {
            return;
        }

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
