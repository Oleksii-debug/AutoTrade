using AutoTrade.Contracts;
using AutoTrade.Host;
using Microsoft.AspNetCore.Http;
using System.Security.Cryptography;
using System.Text;

static void Require(bool condition, string message)
{
    if (!condition) throw new InvalidOperationException(message);
}

static void ExpectInvalid(Action action, string message)
{
    try { action(); }
    catch (InvalidOperationException) { return; }
    throw new InvalidOperationException(message);
}

HostProcessOptions options = HostProcessOptions.Create(
    "http://127.0.0.1:8765/", "host-a", "paper-account", "PAPER");
Require(options.ListenUrl == "http://127.0.0.1:8765", "canonical listen origin mismatch");
Require(options.CredentialTarget == "AutoTrade.HostSession:http://127.0.0.1:8765", "credential target mismatch");
Require(HostProcessOptions.Route(HostApiRoutes.GetState) == "/api/v1/state", "state route diverged");
Require(HostProcessOptions.Route(HostApiRoutes.SubmitCommand) == "/api/v1/commands", "command route diverged");

foreach (string invalid in new[] { "http://example.com:8765/", "https://127.0.0.1:8765/", "http://127.0.0.1:8765/path", "http://user@127.0.0.1:8765/" })
    ExpectInvalid(() => _ = HostProcessOptions.Create(invalid, "host-a", "paper-account", "PAPER"), "unsafe host origin admitted");
ExpectInvalid(() => _ = HostProcessOptions.Create("http://127.0.0.1:8765/", "host-a", "paper-account", "TESTNET"), "invalid runtime environment admitted");

string utc = HostContractTime.FormatUtcInstant(new DateTimeOffset(2026, 10, 1, 8, 30, 0, TimeSpan.FromHours(2)));
Require(utc == "2026-10-01T06:30:00.0000000Z", "UTC instant must normalize offset");

const string Token = "test-token-not-a-real-secret";
byte[] material = Encoding.UTF8.GetBytes("autotrade-ui-session-v1\0" + Token);
string expectedReference;
try { expectedReference = "sid-" + Convert.ToHexString(SHA256.HashData(material)).ToLowerInvariant(); }
finally { CryptographicOperations.ZeroMemory(material); }
Require(WindowsCredentialManagerSessionAuthenticator.PublicSessionReference(Token) == expectedReference, "session reference diverged");

WindowsCredentialManagerSessionAuthenticator authenticator = new(options);
DefaultHttpContext unauthenticated = new();
Require(!authenticator.TryAuthenticate(unauthenticated.Request, out _), "missing headers must fail closed");

IHostAuthorityBoundary boundary = new UnavailableHostAuthorityBoundary();
Require(boundary.Readiness.Status == "BLOCKED", "initial authority must be BLOCKED");
Require(boundary.Readiness.ReasonCodes.Contains("provider_execution_issuer_unavailable", StringComparer.Ordinal), "provider issuer blocker missing");
HostStartupAdmission unavailable = HostStartupAdmission.Evaluate(boundary);
Require(!unavailable.ListenerBindingAuthorized, "unavailable authority authorized bind");
Require(unavailable.ExitCode == HostStartupAdmission.AuthorityUnavailableExitCode, "blocked exit code mismatch");

HostStartupAdmission ready = HostStartupAdmission.Evaluate(new StaticBoundary(new HostAuthorityReadiness("READY", Array.Empty<string>())));
Require(ready.ListenerBindingAuthorized && ready.ExitCode == 0, "exact READY should admit");
HostStartupAdmission contradictory = HostStartupAdmission.Evaluate(new StaticBoundary(new HostAuthorityReadiness("READY", new[] { "provider_execution_issuer_unavailable" })));
Require(!contradictory.ListenerBindingAuthorized, "READY with blocker admitted");
HostStartupAdmission malformed = HostStartupAdmission.Evaluate(new StaticBoundary(new HostAuthorityReadiness("BLOCKED", new[] { "Not Canonical" })));
Require(!malformed.ListenerBindingAuthorized && malformed.ReasonCodes.SequenceEqual(new[] { "host_authority_reason_codes_invalid" }), "malformed reason accepted");
HostStartupAdmission throwing = HostStartupAdmission.Evaluate(new ThrowingBoundary());
Require(!throwing.ListenerBindingAuthorized && throwing.ReasonCodes.SequenceEqual(new[] { "host_authority_probe_failed" }), "probe failure did not fail closed");

Console.WriteLine("AUTOTRADE_HOST_PROCESS_CONTRACT_OK");

sealed class StaticBoundary(HostAuthorityReadiness readiness) : IHostAuthorityBoundary
{
    public HostAuthorityReadiness Readiness { get; } = readiness;
}
sealed class ThrowingBoundary : IHostAuthorityBoundary
{
    public HostAuthorityReadiness Readiness => throw new InvalidOperationException("synthetic authority probe failure");
}
