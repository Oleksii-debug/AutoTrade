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
        IEmergencyHostClient hostClient = DesktopHostClientFactory.Create();
        MainWindow window = new(hostClient);
        MainWindow = window;
        window.Show();
    }
}
