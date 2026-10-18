using System.Windows;
using Velopack;

namespace AutoTrade.Desktop;

public partial class App : Application
{
    [STAThread]
    private static void Main(string[] args)
    {
        VelopackApp.Build()
            .SetAutoApplyOnStartup(false)
            .Run();

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
            // Existing explicitly paired deployments keep their configured host.
            if (!string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("AUTOTRADE_HOST_URI")))
                window = new(DesktopHostClientFactory.Create());
            else
            {
                OwnedProviderFreeRuntime owned = await OwnedProviderFreeRuntime.StartAsync();
                window = new(owned.Client, owned);
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "AutoTrade startup failed", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(2);
            return;
        }
        MainWindow = window;
        window.Show();
    }
}
