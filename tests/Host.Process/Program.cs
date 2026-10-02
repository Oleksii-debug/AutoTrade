using AutoTrade.Contracts;
using AutoTrade.Host;
using Microsoft.AspNetCore.Http;
using System.Security.Cryptography;
using System.IO;
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
    boundary.Readiness.ReasonCodes.Contains(
        "provider_current_authority_bridge_unavailable",
        StringComparer.Ordinal),
    "missing durable provider current-authority bridge must remain explicit");
Require(
    boundary.Readiness.ReasonCodes.Contains(
        "provider_credential_bridge_unavailable",
        StringComparer.Ordinal),
    "missing provider credential bridge must remain explicit");
Require(
    boundary.Readiness.ReasonCodes.Contains(
        "provider_private_socket_not_wired",
        StringComparer.Ordinal),
    "unwired private socket path must remain explicit");

HostStartupAdmission unavailableAdmission = HostStartupAdmission.Evaluate(boundary);
Require(
    !unavailableAdmission.ListenerBindingAuthorized,
    "unavailable financial authority must never authorize listener binding");
Require(
    unavailableAdmission.ExitCode == HostStartupAdmission.AuthorityUnavailableExitCode,
    "blocked startup must use deterministic authority-unavailable exit code");
Require(
    unavailableAdmission.ReasonCodes.Contains(
        "provider_current_authority_bridge_unavailable",
        StringComparer.Ordinal),
    "startup verdict must retain current provider authority blocker");

HostStartupAdmission exactReadyAdmission = HostStartupAdmission.Evaluate(
    new StaticHostAuthorityBoundary(new HostAuthorityReadiness("READY", Array.Empty<string>())));
Require(exactReadyAdmission.ListenerBindingAuthorized, "exact READY authority should admit listener binding");
Require(
    exactReadyAdmission.ExitCode == HostStartupAdmission.SuccessExitCode,
    "exact READY authority must use success exit code");

HostStartupAdmission contradictoryReadyAdmission = HostStartupAdmission.Evaluate(
    new StaticHostAuthorityBoundary(
        new HostAuthorityReadiness(
            "READY",
            new[] { "provider_current_authority_bridge_unavailable" })));
Require(
    !contradictoryReadyAdmission.ListenerBindingAuthorized,
    "READY text with blocker reasons must fail closed");
Require(
    contradictoryReadyAdmission.ReasonCodes.Contains("host_authority_ready_with_blockers", StringComparer.Ordinal),
    "contradictory READY verdict must be explicit");

HostStartupAdmission malformedAdmission = HostStartupAdmission.Evaluate(
    new StaticHostAuthorityBoundary(
        new HostAuthorityReadiness("BLOCKED", new[] { "Not Canonical" })));
Require(
    !malformedAdmission.ListenerBindingAuthorized
        && malformedAdmission.ReasonCodes.SequenceEqual(new[] { "host_authority_reason_codes_invalid" }),
    "malformed authority reason codes must fail closed before bind");

HostStartupAdmission throwingAdmission = HostStartupAdmission.Evaluate(
    new ThrowingHostAuthorityBoundary());
Require(
    !throwingAdmission.ListenerBindingAuthorized
        && throwingAdmission.ReasonCodes.SequenceEqual(new[] { "host_authority_probe_failed" }),
    "authority probe failure must fail closed before bind");


string fenceRoot = Path.Combine(
    Path.GetTempPath(),
    "autotrade-host-fence-tests-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(fenceRoot);
try
{
    HostProcessOptions sameScopeOtherHost = HostProcessOptions.Create(
        "http://127.0.0.1:8766/",
        "host-b",
        "paper-account",
        "PAPER");
    string scopeA = HostLifetimeExclusiveLease.ScopeIdFor(options);
    string scopeB = HostLifetimeExclusiveLease.ScopeIdFor(sameScopeOtherHost);
    Require(
        scopeA == scopeB,
        "lifetime fence must exclude duplicate hosts for the same account/runtime regardless of host id or port");
    Require(
        !scopeA.Contains("paper-account", StringComparison.Ordinal),
        "lifetime fence filename identity must not expose raw account labels");

    using (HostLifetimeExclusiveLease first =
        HostLifetimeExclusiveLease.Acquire(options, fenceRoot))
    {
        bool duplicateBlocked = false;
        try
        {
            using HostLifetimeExclusiveLease _ =
                HostLifetimeExclusiveLease.Acquire(sameScopeOtherHost, fenceRoot);
        }
        catch (HostLifetimeFenceUnavailableException)
        {
            duplicateBlocked = true;
        }
        Require(
            duplicateBlocked,
            "OS lifetime fence must block a second same-account/runtime Host while the first process lease is held");

        HostProcessOptions otherAccount = HostProcessOptions.Create(
            "http://127.0.0.1:8767/",
            "host-c",
            "paper-account-2",
            "PAPER");
        using HostLifetimeExclusiveLease other =
            HostLifetimeExclusiveLease.Acquire(otherAccount, fenceRoot);
        Require(
            other.ScopeId != first.ScopeId,
            "independent account scopes must not alias one lifetime fence");
    }

    using HostLifetimeExclusiveLease reacquired =
        HostLifetimeExclusiveLease.Acquire(sameScopeOtherHost, fenceRoot);
    Require(
        reacquired.ScopeId == scopeA,
        "same-host handoff may acquire the OS fence only after the prior lease is released");
}
finally
{
    try
    {
        Directory.Delete(fenceRoot, recursive: true);
    }
    catch (IOException)
    {
        // Windows can defer final handle release very briefly; cleanup is non-authoritative.
    }
    catch (UnauthorizedAccessException)
    {
        // Test cleanup must not turn a passed authority contract into a false failure.
    }
}

Console.WriteLine("AUTOTRADE_HOST_PROCESS_CONTRACT_OK");

sealed class StaticHostAuthorityBoundary(HostAuthorityReadiness readiness) : IHostAuthorityBoundary
{
    public HostAuthorityReadiness Readiness { get; } = readiness;
}

sealed class ThrowingHostAuthorityBoundary : IHostAuthorityBoundary
{
    public HostAuthorityReadiness Readiness =>
        throw new InvalidOperationException("synthetic authority probe failure");
}
