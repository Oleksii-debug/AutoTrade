using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using System.Threading;
using AutoTrade.Host;

internal static class BybitAuthenticatedReadDurabilityContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        ProviderAuthenticatedReadRequest request = Request();
        ProviderCurrentAuthenticatedReadAuthority authority = Authority();
        FixedAuthority current = new(authority);
        RecordingCredentials credentials = new();
        BlockingDurability durability = new();

        using ProviderIssuerAuthority issuer =
            ProviderIssuerAuthority.CreateProcessAuthority(
                new DateTimeOffset(2026, 10, 2, 11, 0, 0, TimeSpan.Zero));
        BybitAuthenticatedReadClient client = new(
            current,
            credentials,
            issuer,
            durability);

        bool blocked = false;
        try
        {
            client.ExecuteAsync(request, CancellationToken.None)
                .GetAwaiter()
                .GetResult();
        }
        catch (ProviderIssuerAuthorityException error)
            when (error.Message.Contains(
                "simulated durable Prepared refusal",
                StringComparison.Ordinal))
        {
            blocked = true;
        }
        Check(blocked, "durability refusal must block before provider wire execution");
        Check(
            durability.CommitCalls == 1,
            "exactly one durable Prepared attempt must be requested");
        Check(
            credentials.ReadResolveCalls == 1,
            "credential must not be re-resolved for send before durable Prepared succeeds");
        Check(
            current.RequireCurrentCalls == 1,
            "current authority must not advance into post-durability wire phase on refusal");
        Check(
            durability.LastEvidence is not null,
            "durability boundary did not receive signed Prepared evidence");

        ProviderAuthenticatedReadPreparedEvidence evidence =
            durability.LastEvidence!;
        ProviderIssuerVerifier.RequireValidReadAttempt(
            evidence.IssuerSession,
            evidence.Attempt,
            evidence.IssuerSession.SessionIdentity,
            evidence.IssuerSession.PublicKeySha256);
        Check(
            evidence.Attempt.Subject.CredentialGeneration == 7,
            "signed Prepared attempt did not bind exact credential generation");
        Check(
            BybitAuthenticatedReadClient.CanonicalQuery(evidence.Query)
                == "category=linear&symbol=BTC+USDT",
            "durable Prepared evidence did not retain exact query");
        Check(
            credentials.LastSecret is not null
                && credentials.LastSecret.All(item => item == 0),
            "first-phase credential bytes were not zeroed before durability boundary");

        foreach (FieldInfo field in typeof(ProviderAuthenticatedReadPreparedEvidence)
            .GetFields(BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public))
        {
            Check(
                field.FieldType != typeof(byte[])
                    && field.FieldType != typeof(ResolvedProviderCredential)
                    && field.FieldType != typeof(BybitCredentialMaterial),
                "durable Prepared evidence must not retain provider secret material");
        }

        // The failed durability barrier must also close the Host issuer attempt;
        // otherwise a later caller could attach response bytes to an attempt that
        // was never durably admitted before the wire.
        bool abandoned = false;
        try
        {
            issuer.IssueAuthenticatedReadReceipt(
                evidence.Attempt,
                200,
                Encoding.UTF8.GetBytes("{\"retCode\":0}"),
                new DateTimeOffset(2026, 10, 2, 11, 0, 1, TimeSpan.Zero));
        }
        catch (ProviderIssuerAuthorityException)
        {
            abandoned = true;
        }
        Check(abandoned, "durability refusal must abandon the signed read attempt");

        IProviderAuthenticatedReadDurabilityBoundary unavailable =
            new UnavailableProviderAuthenticatedReadDurabilityBoundary();
        bool unavailableBlocked = false;
        try
        {
            unavailable.CommitPrepared(evidence);
        }
        catch (ProviderIssuerAuthorityException)
        {
            unavailableBlocked = true;
        }
        Check(
            unavailableBlocked,
            "product durability boundary must remain fail-closed until journal bridge exists");
    }

    private sealed class FixedAuthority
        : IProviderAuthenticatedReadAuthorityBoundary
    {
        internal FixedAuthority(ProviderCurrentAuthenticatedReadAuthority authority)
        {
            Authority = authority;
        }

        internal ProviderCurrentAuthenticatedReadAuthority Authority { get; }
        internal int RequireCurrentCalls { get; private set; }
        internal int StillCurrentCalls { get; private set; }

        public ProviderCurrentAuthenticatedReadAuthority RequireCurrent(
            ProviderAuthenticatedReadRequest request)
        {
            RequireCurrentCalls++;
            return Authority;
        }

        public void RequireStillCurrent(
            ProviderCurrentAuthenticatedReadAuthority authority)
        {
            StillCurrentCalls++;
            if (!ReferenceEquals(authority, Authority))
            {
                throw new ProviderIssuerAuthorityException(
                    "test current authority identity changed");
            }
        }
    }

    private sealed class RecordingCredentials : IProviderCredentialMaterialSource
    {
        internal int ReadResolveCalls { get; private set; }
        internal byte[]? LastSecret { get; private set; }

        public ResolvedProviderCredential Resolve(
            ProviderCurrentRouteAuthority authority,
            string credentialHandleId,
            string purpose) =>
            throw new InvalidOperationException("route credential path is not under test");

        public ResolvedProviderCredential Resolve(
            ProviderCurrentAuthenticatedReadAuthority authority,
            string credentialHandleId,
            string purpose)
        {
            ReadResolveCalls++;
            LastSecret = Encoding.ASCII.GetBytes(
                "{\"api_key\":\"key-123\",\"api_secret\":\"secret-456\"}");
            return new ResolvedProviderCredential(
                credentialHandleId,
                generation: 7,
                purpose,
                LastSecret);
        }
    }

    private sealed class BlockingDurability
        : IProviderAuthenticatedReadDurabilityBoundary
    {
        internal int CommitCalls { get; private set; }
        internal ProviderAuthenticatedReadPreparedEvidence? LastEvidence { get; private set; }

        public ProviderAuthenticatedReadDurabilityReceipt CommitPrepared(
            ProviderAuthenticatedReadPreparedEvidence evidence)
        {
            CommitCalls++;
            LastEvidence = evidence;
            throw new ProviderIssuerAuthorityException(
                "simulated durable Prepared refusal");
        }
    }

    private static ProviderAuthenticatedReadRequest Request() =>
        new(
            ProviderId: "BYBIT",
            AccountId: "acct-1",
            RuntimeEnvironment: "PAPER",
            ProviderEnvironment: "TESTNET",
            Endpoint: "/v5/account/wallet-balance",
            Surface: "AUTHENTICATED_READ",
            PermissionScope: "ACCOUNT_READ",
            InstrumentVersion: "instrument-version-1",
            QueryDigest: "sha256:" + new string('1', 64),
            CredentialHandleId: "credential-handle-1",
            Query: new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["symbol"] = "BTC USDT",
                ["category"] = "linear",
            });

    private static ProviderCurrentAuthenticatedReadAuthority Authority() =>
        new(
            ProviderId: "BYBIT",
            AccountId: "acct-1",
            EntityId: "entity-1",
            RuntimeEnvironment: "PAPER",
            ProviderEnvironment: "TESTNET",
            Endpoint: "/v5/account/wallet-balance",
            Surface: "AUTHENTICATED_READ",
            PermissionScope: "ACCOUNT_READ",
            DataEntitlement: "ACCOUNT_BALANCES",
            InstrumentVersion: "instrument-version-1",
            QueryDigest: "sha256:" + new string('1', 64),
            EndpointRuleIdentity: "sha256:" + new string('2', 64),
            CredentialHandleId: "credential-handle-1",
            CapabilityId: "capability-1",
            QualificationId: "qualification-1",
            QualificationBuildId: "qualification-build-1",
            AdapterBuildIdentity: "adapter-build-1",
            NetworkPolicyIdentity: "sha256:" + new string('3', 64),
            TransportIdentity: "provider-transport:https-v1",
            Query: new Dictionary<string, string>(StringComparer.Ordinal)
            {
                ["symbol"] = "BTC USDT",
                ["category"] = "linear",
            });

    private static void Check(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }
}
