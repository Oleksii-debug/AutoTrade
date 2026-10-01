using AutoTrade.Contracts;
using AutoTrade.Host;
using Microsoft.AspNetCore.Http;
using System.Security.Cryptography;
using System.Text;

static void Require(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

static void ExpectInvalid(Action action, string message)
{
    try
    {
        action();
    }
    catch (InvalidOperationException)
    {
        return;
    }
    throw new InvalidOperationException(message);
}

HostProcessOptions options = HostProcessOptions.Create(
    "http://127.0.0.1:8765/",
    "host-a",
    "paper-account",
    "PAPER");
Require(options.ListenUrl == "http://127.0.0.1:8765", "canonical listen origin mismatch");
Require(
    options.CredentialTarget == "AutoTrade.HostSession:http://127.0.0.1:8765",
    "credential target does not match Desktop contract");
Require(
    HostProcessOptions.Route(HostApiRoutes.GetState) == "/api/v1/state",
    "state route diverged from generated host contract");
Require(
    HostProcessOptions.Route(HostApiRoutes.SubmitCommand) == "/api/v1/commands",
    "command route diverged from generated host contract");

const string OperationId = "57ad5f31-72fa-4adb-b088-89f4e30ee9d8";
Require(
    HostProcessOptions.OperationRoutePattern.Replace(
        "{operation_id}",
        Uri.EscapeDataString(OperationId),
        StringComparison.Ordinal)
    == "/" + HostApiRoutes.GetOperation(OperationId),
    "operation route pattern diverged from generated host contract");

foreach (string invalid in new[]
{
    "http://example.com:8765/",
    "https://127.0.0.1:8765/",
    "http://127.0.0.1:8765/path",
    "http://user@127.0.0.1:8765/",
})
{
    ExpectInvalid(
        () => _ = HostProcessOptions.Create(invalid, "host-a", "paper-account", "PAPER"),
        "unsafe host origin was admitted: " + invalid);
}

ExpectInvalid(
    () => _ = HostProcessOptions.Create(
        "http://127.0.0.1:8765/",
        "host-a",
        "paper-account",
        "TESTNET"),
    "non-canonical runtime environment was admitted");

string utc = HostContractTime.FormatUtcInstant(
    new DateTimeOffset(2026, 10, 1, 8, 30, 0, TimeSpan.FromHours(2)));
Require(
    utc == "2026-10-01T06:30:00.0000000Z",
    "UTC instant must use canonical Z suffix and normalize offset");

const string Token = "test-token-not-a-real-secret";
byte[] material = Encoding.UTF8.GetBytes("autotrade-ui-session-v1\0" + Token);
string expectedReference;
try
{
    expectedReference = "sid-" + Convert.ToHexString(SHA256.HashData(material)).ToLowerInvariant();
}
finally
{
    CryptographicOperations.ZeroMemory(material);
}
Require(
    WindowsCredentialManagerSessionAuthenticator.PublicSessionReference(Token)
        == expectedReference,
    "host public session reference diverged from Desktop contract");

WindowsCredentialManagerSessionAuthenticator authenticator = new(options);
DefaultHttpContext unauthenticated = new();
Require(
    !authenticator.TryAuthenticate(unauthenticated.Request, out _),
    "missing session headers must fail closed without credential access");

IHostAuthorityBoundary boundary = new UnavailableHostAuthorityBoundary();
Require(boundary.Readiness.Status == "BLOCKED", "initial host authority must be BLOCKED");
Require(
    boundary.Readiness.ReasonCodes.Contains("provider_execution_issuer_unavailable", StringComparer.Ordinal),
    "missing provider issuer must remain explicit");

Console.WriteLine("AUTOTRADE_HOST_PROCESS_CONTRACT_OK");
