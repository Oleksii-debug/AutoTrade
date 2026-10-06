using System.Windows;

namespace AutoTrade.Desktop;

public partial class App : Application
{
    [STAThread]
    private static void Main(string[] args)
    {
        App app = new();
        app.InitializeComponent();
        app.Run();
    }

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        DesktopHostConnection connection = DesktopHostClientFactory.CreateConnection();
        MainWindow window = new(connection.Client, connection.SessionProvider);
        MainWindow = window;
        window.Show();
    }
}
