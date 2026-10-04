using AutoTrade.Contracts;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;

namespace AutoTrade.Desktop;

/// <summary>Owns the installed ZERO host; financial commands remain in the existing host.</summary>
internal sealed class OwnedProviderFreeRuntime : IEmergencyHostSessionProvider, IAsyncDisposable
{
    private readonly Process _process;
    private readonly HttpClient _http;
    private readonly EmergencyHostSession _session;
    private readonly Task _stdoutDrain;
    private readonly Task _stderrDrain;
    public Uri Origin => _session.Origin;
    public string DataDirectory { get; }
    public string CookieToken => _session.Token;
    public IEmergencyHostClient Client { get; }

    private OwnedProviderFreeRuntime(Process process, HttpClient http, EmergencyHostSession session, string data,
        Task stdoutDrain, Task stderrDrain)
    {
        _process = process;
        _http = http;
        _session = session.Validated();
        _stdoutDrain = stdoutDrain;
        _stderrDrain = stderrDrain;
        DataDirectory = data;
        Client = new AuthenticatedEmergencyHostClient(http, Origin, this,
            new WindowsCredentialManagerPendingCommandStore("AutoTrade.ZERO:pending-emergency-command-v1"));
    }

    public EmergencyHostSession GetSession() => _session;

    private static async Task DrainRedirectedPipeAsync(StreamReader reader)
    {
        char[] buffer = new char[4096];
        try
        {
            while (await reader.ReadAsync(buffer, 0, buffer.Length) != 0) { }
        }
        catch (IOException) { }
        catch (ObjectDisposedException) { }
    }

    public static async Task<OwnedProviderFreeRuntime> StartAsync()
    {
        string installed = AppContext.BaseDirectory;
        string product = Path.Combine(installed, "product");
        InstalledCandidateInventory.Verify(installed);
        string python = Path.Combine(installed, "runtime", "python", "python.exe");
        if (!File.Exists(python) || !File.Exists(Path.Combine(product, "SOURCE_REVISION")))
            throw new InvalidOperationException("Installed Python runtime or frozen product files are missing. Repair the AutoTrade package.");
        string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AutoTrade-ZERO");
        Directory.CreateDirectory(data);
        ProcessStartInfo start = new(python)
        {
            WorkingDirectory = product,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
        };
        foreach (string arg in new[] {"-B", "-m", "mvp.autotrade_mvp.product_runtime", "--data-dir", data,
            "--port", "0", "--no-browser", "--desktop-child", "--parent-pid", Environment.ProcessId.ToString()})
            start.ArgumentList.Add(arg);
        Process process = Process.Start(start) ?? throw new InvalidOperationException("The installed host could not start.");
        // Drain redirected pipes without retaining unbounded untrusted child output in memory.
        Task stderrDrain = DrainRedirectedPipeAsync(process.StandardError);
        Task stdoutDrain = Task.CompletedTask;
        HttpClient http = new(new HttpClientHandler {AllowAutoRedirect = false, UseCookies = false})
        { Timeout = TimeSpan.FromSeconds(10) };
        try
        {
            using CancellationTokenSource deadline = new(TimeSpan.FromSeconds(30));
            string? line = await process.StandardOutput.ReadLineAsync(deadline.Token);
            stdoutDrain = DrainRedirectedPipeAsync(process.StandardOutput);
            const string prefix = "AutoTrade ZERO: ";
            if (line is null || !line.StartsWith(prefix, StringComparison.Ordinal))
                throw new InvalidOperationException("The installed host did not become ready. Check package files, data-directory permissions and another running AutoTrade instance.");
            Uri launch = new(line[prefix.Length..], UriKind.Absolute);
            Uri origin = new(launch.GetLeftPart(UriPartial.Authority) + "/");
            if (launch.Scheme != "http" || launch.Host != "127.0.0.1" || launch.Port <= 0
                || launch.UserInfo.Length != 0
                || launch.AbsolutePath != "/" || launch.Query.Length != 0 || !launch.Fragment.StartsWith("#pair=", StringComparison.Ordinal))
                throw new InvalidOperationException("Host readiness origin differs from the installed ZERO authority.");
            string code = launch.Fragment[6..];
            using HttpRequestMessage pair = new(HttpMethod.Post, new Uri(origin, HostApiRoutes.PairLocalSession));
            pair.Headers.Add("Origin", origin.GetLeftPart(UriPartial.Authority));
            pair.Content = new StringContent(JsonSerializer.Serialize(new {pairing_code = code}), Encoding.UTF8, "application/json");
            // The canonical pairing endpoint requires this exact media type.
            pair.Content.Headers.ContentType!.CharSet = null;
            using HttpResponseMessage response = await http.SendAsync(pair, deadline.Token);
            response.EnsureSuccessStatusCode();
            string cookie = response.Headers.GetValues("Set-Cookie").Single();
            const string cookieName = "AutoTradeSession=";
            if (!cookie.StartsWith(cookieName, StringComparison.Ordinal))
                throw new InvalidOperationException("Host pairing did not return the expected session cookie.");
            string token = cookie[cookieName.Length..cookie.IndexOf(';')];
            // The reusable token stays in process memory and WebView's HttpOnly cookie.
            return new(process, http, new EmergencyHostSession("local-owner", token, origin), data,
                stdoutDrain, stderrDrain);
        }
        catch
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync();
            await Task.WhenAll(stdoutDrain, stderrDrain);
            process.Dispose(); http.Dispose();
            throw;
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (!_process.HasExited)
        {
            // The owned pipe requests the existing production-host drain. It does
            // not bypass the authenticated financial command API.
            await _process.StandardInput.WriteLineAsync("STOP");
            await _process.StandardInput.FlushAsync();
            using CancellationTokenSource deadline = new(TimeSpan.FromSeconds(30));
            try { await _process.WaitForExitAsync(deadline.Token); }
            catch (OperationCanceledException)
            {
                _process.Kill(entireProcessTree: true);
                await _process.WaitForExitAsync();
            }
        }
        await Task.WhenAll(_stdoutDrain, _stderrDrain);
        _process.Dispose(); _http.Dispose();
    }
}
