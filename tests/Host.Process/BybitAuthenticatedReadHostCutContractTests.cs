using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using AutoTrade.Host;

internal static class BybitAuthenticatedReadHostCutContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        ProviderAuthenticatedReadRequest request = Request("TESTNET");
        ProviderCurrentAuthenticatedReadAuthority authority = Authority("TESTNET");

        IProviderAuthenticatedReadAuthorityBoundary unavailable =
            new UnavailableProviderAuthenticatedReadAuthorityBoundary();
        ExpectFailure(
            () => unavailable.RequireCurrent(request),
            "missing current authenticated-read authority must fail closed");

        IProviderCredentialMaterialSource credentials =
            new UnavailableProviderCredentialMaterialSource();
        ExpectFailure(
            () => credentials.Resolve(
                authority,
                "credential-handle-1",
                "READ"),
            "missing authenticated-read credential bridge must fail closed");

        Check(
            !typeof(BybitAuthenticatedReadClient).IsPublic,
            "credential-bearing authenticated-read client must remain Host-internal");
        Check(
            !typeof(IProviderAuthenticatedReadAuthorityBoundary).IsPublic,
            "authenticated-read current-authority bridge must remain Host-internal");

        HashSet<string> requestProperties = new(
            typeof(ProviderAuthenticatedReadRequest)
                .GetProperties()
                .Select(item => item.Name),
            StringComparer.Ordinal);
        foreach (string prohibited in new[]
        {
            "EntityId",
            "DataEntitlement",
            "EndpointRuleIdentity",
            "CapabilityId",
            "QualificationId",
            "QualificationBuildId",
            "AdapterBuildIdentity",
            "NetworkPolicyIdentity",
            "TransportIdentity",
            "CredentialGeneration",
        })
        {
            Check(
                !requestProperties.Contains(prohibited),
                "read request must not accept caller-selected terminal authority " + prohibited);
        }

        MethodInfo execute = typeof(BybitAuthenticatedReadClient)
            .GetMethod(
                "ExecuteAsync",
                BindingFlags.Instance | BindingFlags.NonPublic)
            ?? throw new InvalidOperationException(
                "authenticated-read ExecuteAsync is missing");
        foreach (ParameterInfo parameter in execute.GetParameters())
        {
            Type type = parameter.ParameterType;
            Check(
                type != typeof(HttpClient)
                    && type != typeof(HttpMessageInvoker)
                    && !typeof(Delegate).IsAssignableFrom(type),
                "production provider-origin read cut must not accept injected wire callbacks");
        }

        Check(
            BybitAuthenticatedReadClient.CanonicalQuery(
                new Dictionary<string, string>(StringComparer.Ordinal)
                {
                    ["symbol"] = "BTC USDT",
                    ["category"] = "linear",
                })
            == "category=linear&symbol=BTC+USDT",
            "Host Bybit query canonicalization diverged from provider adapter");
        Check(
            BybitAuthenticatedReadClient.ProviderBaseUri("LIVE", "MAINNET").Host
                == "api.bybit.com",
            "MAINNET Host origin drifted");
        Check(
            BybitAuthenticatedReadClient.ProviderBaseUri("PAPER", "TESTNET").Host
                == "api-testnet.bybit.com",
            "TESTNET Host origin drifted");
        Check(
            BybitAuthenticatedReadClient.ProviderBaseUri("PAPER", "DEMO").Host
                == "api-demo.bybit.com",
            "DEMO Host origin drifted");
        ExpectFailure(
            () => BybitAuthenticatedReadClient.ProviderBaseUri("PAPER", "MAINNET"),
            "runtime/provider-domain alias must fail closed");

        BybitAuthenticatedReadClient.RequireRequestMatchesAuthority(
            request,
            authority);
        ExpectFailure(
            () => BybitAuthenticatedReadClient.RequireRequestMatchesAuthority(
                request,
                authority with { ProviderEnvironment = "DEMO" }),
            "TESTNET request must not bind to DEMO current authority");
        ExpectFailure(
            () => BybitAuthenticatedReadClient.RequireRequestMatchesAuthority(
                request,
                authority with
                {
                    Query = new Dictionary<string, string>(StringComparer.Ordinal)
                    {
                        ["category"] = "spot",
                        ["symbol"] = "BTC USDT",
                    }
                }),
            "current authority must bind the exact query sent on the wire");

        byte[] credentialBytes =
            Encoding.ASCII.GetBytes(
                "{\"api_key\":\"key-123\",\"api_secret\":\"secret-456\"}");
        try
        {
            using BybitCredentialMaterial credential =
                BybitCredentialMaterial.Parse(credentialBytes);
            using HttpRequestMessage outbound =
                BybitAuthenticatedReadClient.BuildRequestMessage(
                    authority,
                    credential,
                    1700000000123);

            Check(
                outbound.Method == HttpMethod.Get,
                "authenticated provider read must be an HTTP GET");
            Check(
                outbound.RequestUri?.AbsoluteUri
                    == "https://api-testnet.bybit.com/v5/account/wallet-balance?category=linear&symbol=BTC+USDT",
                "authenticated provider read URI drifted");
            Check(
                Header(outbound, "X-BAPI-API-KEY") == "key-123",
                "Bybit API key header drifted");
            Check(
                Header(outbound, "X-BAPI-TIMESTAMP") == "1700000000123",
                "Bybit timestamp header drifted");
            Check(
                Header(outbound, "X-BAPI-RECV-WINDOW") == "5000",
                "Bybit receive-window header drifted");
            Check(
                Header(outbound, "X-BAPI-SIGN")
                    == "6dccfc3ca54f7589ef047ff4f1a50a8882514b0d19428fdf014e9c1d111201c8",
                "Bybit authenticated-read HMAC vector drifted");
            Check(
                !outbound.RequestUri!.AbsoluteUri.Contains(
                    "secret-456",
                    StringComparison.Ordinal),
                "provider secret must never enter the request URI");
        }
        finally
        {
            CryptographicOperations.ZeroMemory(credentialBytes);
        }
    }

    private static ProviderAuthenticatedReadRequest Request(
        string providerEnvironment) =>
        new(
            ProviderId: "BYBIT",
            AccountId: "acct-1",
            RuntimeEnvironment: providerEnvironment == "MAINNET" ? "LIVE" : "PAPER",
            ProviderEnvironment: providerEnvironment,
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

    private static ProviderCurrentAuthenticatedReadAuthority Authority(
        string providerEnvironment) =>
        new(
            ProviderId: "BYBIT",
            AccountId: "acct-1",
            EntityId: "entity-1",
            RuntimeEnvironment: providerEnvironment == "MAINNET" ? "LIVE" : "PAPER",
            ProviderEnvironment: providerEnvironment,
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

    private static string Header(HttpRequestMessage request, string name) =>
        request.Headers.TryGetValues(name, out IEnumerable<string>? values)
            ? values.Single()
            : throw new InvalidOperationException("missing header " + name);

    private static void Check(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }

    private static void ExpectFailure(Action action, string message)
    {
        try
        {
            action();
        }
        catch (ProviderIssuerAuthorityException)
        {
            return;
        }
        throw new InvalidOperationException(message);
    }
}
