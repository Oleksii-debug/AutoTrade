using System.Net;
using System.Text;
using System.Text.Json;
using AutoTrade.Desktop;

namespace DesktopClientContracts;

internal static class Program
{
    static HttpResponseMessage Json(HttpStatusCode status, object value) =>
        new(status)
        {
            Content = new StringContent(
                JsonSerializer.Serialize(value),
                Encoding.UTF8,
                "application/json"),
        };
    
    static string NowUtc() =>
        DateTimeOffset.UtcNow.AddSeconds(-1).UtcDateTime.ToString(
            "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            System.Globalization.CultureInfo.InvariantCulture);
    
    static object Snapshot(string token, string version = "0") => new
    {
        state_version = version,
        event_cursor = version,
        server_time = NowUtc(),
        host_id = "host-local-1",
        account_id = "paper-account-1",
        environment = "PAPER",
        permission_summary = new { actor = "owner", session = token, role = "OWNER" },
        connection_freshness = new { host = "CURRENT", as_of = NowUtc() },
        portfolio = new { },
        risk = new { },
        strategy = new { },
        jobs = Array.Empty<object>(),
        reason_codes = Array.Empty<string>(),
    };
    
    static void AssertAuth(HttpRequestMessage request, string token)
    {
        Check.True(
            request.Headers.Authorization?.Scheme == "AutoTrade-Session",
            "request must use AutoTrade-Session authentication scheme");
        Check.True(
            request.Headers.Authorization?.Parameter == token,
            "request session token changed");
        Check.True(
            request.Headers.TryGetValues("X-AutoTrade-Actor", out IEnumerable<string>? actors)
                && actors.Single() == "owner",
            "request actor header is missing or changed");
    }
    
    static async Task CanonicalStatusAndOperationTest()
    {
        const string token = "session-token-a";
        MutableSessionProvider sessions =
            new(new EmergencyHostSession("owner", token));
        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            AssertAuth(request, token);
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath == "/api/v1/state")
            {
                return Json(HttpStatusCode.OK, Snapshot(token, "7"));
            }
    
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath.StartsWith(
                    "/api/v1/operations/",
                    StringComparison.Ordinal))
            {
                string operationId = request.RequestUri.AbsolutePath.Split('/').Last();
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        operation_id = operationId,
                        phase = "SUCCEEDED",
                        started_at = NowUtc(),
                        updated_at = NowUtc(),
                        affected_refs = Array.Empty<string>(),
                        evidence = Array.Empty<object>(),
                        remaining_uncertainty = Array.Empty<string>(),
                    });
            }
    
            throw new InvalidOperationException("unexpected request " + request.RequestUri);
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        EmergencyHostStatus status = await client.GetStatusAsync(CancellationToken.None);
        Check.True(status.Connected, "authenticated snapshot must be connected");
        Check.True(status.HostId == "host-local-1", "host identity changed");
        Check.True(status.AccountId == "paper-account-1", "account identity changed");
        Check.True(status.Environment == "PAPER", "environment identity changed");
        Check.True(status.StateVersion == "7", "state version changed");
    
        const string op = "22222222-2222-2222-2222-222222222222";
        EmergencyOperationStatus operation =
            await client.GetOperationAsync(op, CancellationToken.None);
        Check.True(
            operation.State == EmergencyOperationState.Succeeded,
            "SUCCEEDED phase was not preserved");
        Check.True(operation.DurableBlockConfirmed, "succeeded block must be durable");
        Check.True(
            operation.InFlightActions == InFlightActionState.Unknown,
            "operation success must not fabricate provider in-flight absence");
    }
    
    static async Task AmbiguousPostExactRetryTest()
    {
        const string token = "session-token-b";
        MutableSessionProvider sessions =
            new(new EmergencyHostSession("owner", token));
        List<string> commandBodies = [];
        int postCount = 0;
        const string operationId = "33333333-3333-3333-3333-333333333333";
    
        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            AssertAuth(request, token);
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath == "/api/v1/state")
            {
                return Json(HttpStatusCode.OK, Snapshot(token));
            }
    
            if (request.Method == HttpMethod.Post
                && request.RequestUri!.AbsolutePath == "/api/v1/commands")
            {
                postCount++;
                string body = await request.Content!.ReadAsStringAsync(cancellationToken);
                commandBodies.Add(body);
                if (postCount == 1)
                {
                    throw new HttpRequestException("response lost after durable commit");
                }
    
                using JsonDocument parsed = JsonDocument.Parse(body);
                string commandId =
                    parsed.RootElement.GetProperty("command_id").GetString()!;
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        command_id = commandId,
                        status = "ACCEPTED",
                        state_version = "1",
                        reason_codes = Array.Empty<string>(),
                        field_errors = Array.Empty<object>(),
                        operation_id = operationId,
                    });
            }
    
            if (request.Method == HttpMethod.Get
                && request.RequestUri!.AbsolutePath
                    == "/api/v1/operations/" + operationId)
            {
                return Json(
                    HttpStatusCode.OK,
                    new
                    {
                        operation_id = operationId,
                        phase = "QUEUED",
                        started_at = NowUtc(),
                        updated_at = NowUtc(),
                        affected_refs = Array.Empty<string>(),
                        evidence = Array.Empty<object>(),
                        remaining_uncertainty = new[]
                        {
                            "financial_outcome_not_completed",
                        },
                    });
            }
    
            throw new InvalidOperationException("unexpected request " + request.RequestUri);
        });
    
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        string? uncertainCommandId = null;
        try
        {
            await client.BlockNewExposureAsync(CancellationToken.None);
            throw new InvalidOperationException("first lost response must remain uncertain");
        }
        catch (EmergencyCommandUncertainException error)
        {
            uncertainCommandId = error.CommandId;
        }
    
        EmergencyCommandResult recovered =
            await client.BlockNewExposureAsync(CancellationToken.None);
        Check.True(recovered.Accepted, "exact retry must recover durable acceptance");
        Check.True(
            recovered.OperationId == operationId,
            "exact retry recovered the wrong operation");
        Check.True(postCount == 2, "expected exactly one original send and one exact recovery send");
        Check.True(
            commandBodies.Count == 2 && commandBodies[0] == commandBodies[1],
            "recovery changed command/idempotency/scope/session payload");
    
        using JsonDocument body = JsonDocument.Parse(commandBodies[0]);
        Check.True(
            body.RootElement.GetProperty("command_id").GetString() == uncertainCommandId,
            "surfaced uncertain command identity differs from retried command");
    }
    
    static async Task UncertainCommandCannotRetargetSessionTest()
    {
        const string originalToken = "session-token-c";
        MutableSessionProvider sessions =
            new(new EmergencyHostSession("owner", originalToken));
        int posts = 0;
    
        DelegateHandler handler = new(async (request, _, cancellationToken) =>
        {
            if (request.Method == HttpMethod.Get)
            {
                AssertAuth(request, originalToken);
                return Json(HttpStatusCode.OK, Snapshot(originalToken));
            }
    
            posts++;
            _ = await request.Content!.ReadAsStringAsync(cancellationToken);
            throw new HttpRequestException("response unknown");
        });
    
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => client.BlockNewExposureAsync(CancellationToken.None),
            "first ambiguous send must be uncertain");
        Check.True(posts == 1, "first command was not sent exactly once");
    
        sessions.Session = new EmergencyHostSession("owner", "different-session-token");
        await Check.ThrowsAsync<EmergencyCommandUncertainException>(
            () => client.BlockNewExposureAsync(CancellationToken.None),
            "changed session must not retarget unresolved command");
        Check.True(
            posts == 1,
            "client sent unresolved command with a different session instead of failing closed");
    }
    
    static async Task ScopeAndCanonicalResponseFailureTest()
    {
        const string token = "session-token-d";
        MutableSessionProvider sessions =
            new(new EmergencyHostSession("owner", token));
        DelegateHandler handler = new((request, _, _) =>
        {
            AssertAuth(request, token);
            return Task.FromResult(
                Json(
                    HttpStatusCode.OK,
                    new
                    {
                        state_version = "01",
                        event_cursor = "0",
                        server_time = NowUtc(),
                        host_id = "host-local-1",
                        account_id = "paper-account-1",
                        environment = "PAPER",
                        permission_summary = new { actor = "owner", session = token },
                        connection_freshness = new { host = "CURRENT" },
                        portfolio = new { },
                        risk = new { },
                        strategy = new { },
                        jobs = Array.Empty<object>(),
                        reason_codes = Array.Empty<string>(),
                    }));
        });
        AuthenticatedEmergencyHostClient client = new(
            new HttpClient(handler),
            new Uri("http://127.0.0.1:8765/"),
            sessions);
    
        await Check.ThrowsAsync<InvalidOperationException>(
            () => client.GetStatusAsync(CancellationToken.None),
            "non-canonical sequence must fail closed");
    }

    public static async Task Main()
    {
        await CanonicalStatusAndOperationTest();
        await AmbiguousPostExactRetryTest();
        await UncertainCommandCannotRetargetSessionTest();
        await ScopeAndCanonicalResponseFailureTest();
        Console.WriteLine("Desktop authenticated host-client contract tests passed.");
    }
}

internal static class Check
{
    public static void True(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    public static async Task ThrowsAsync<T>(Func<Task> action, string message)
        where T : Exception
    {
        try
        {
            await action();
        }
        catch (T)
        {
            return;
        }

        throw new InvalidOperationException(message);
    }
}

internal sealed class MutableSessionProvider : IEmergencyHostSessionProvider
{
    public EmergencyHostSession Session { get; set; }

    public MutableSessionProvider(EmergencyHostSession session)
    {
        Session = session;
    }

    public EmergencyHostSession GetSession() => Session;
}

internal sealed class DelegateHandler : HttpMessageHandler
{
    private readonly Func<HttpRequestMessage, int, CancellationToken, Task<HttpResponseMessage>> _handler;
    private int _count;

    public DelegateHandler(
        Func<HttpRequestMessage, int, CancellationToken, Task<HttpResponseMessage>> handler)
    {
        _handler = handler;
    }

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        int call = Interlocked.Increment(ref _count);
        return _handler(request, call, cancellationToken);
    }
}
