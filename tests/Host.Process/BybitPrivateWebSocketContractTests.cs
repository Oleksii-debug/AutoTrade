using System.Reflection;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using AutoTrade.Host;

internal static class BybitPrivateWebSocketContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        TestCredentialEnvelope();
        TestAuthenticationWireBytes();
        TestRouteBinding();
        TestControlAcknowledgements();
        TestExecutionFrameShape();
        TestFailClosedBridges();
        TestCallerCannotSelectTerminalAuthority();
        TestFrameLifetime();
    }

    private static void TestCredentialEnvelope()
    {
        byte[] apiKey = Encoding.ASCII.GetBytes("test-api-key");
        byte[] apiSecret = Encoding.ASCII.GetBytes("test-api-secret");
        byte[] payload = BybitCredentialMaterial.CanonicalPayload(apiKey, apiSecret);
        try
        {
            Check(
                Encoding.ASCII.GetString(payload)
                    == "{\"api_key\":\"test-api-key\",\"api_secret\":\"test-api-secret\"}",
                "BYBIT canonical credential payload bytes changed");
            using BybitCredentialMaterial parsed = BybitCredentialMaterial.Parse(payload);
            Check(
                parsed.ApiKey.SequenceEqual(apiKey),
                "BYBIT credential parser changed api_key bytes");
            Check(
                parsed.ApiSecret.SequenceEqual(apiSecret),
                "BYBIT credential parser changed api_secret bytes");
        }
        finally
        {
            CryptographicOperations.ZeroMemory(apiKey);
            CryptographicOperations.ZeroMemory(apiSecret);
            CryptographicOperations.ZeroMemory(payload);
        }

        ExpectFailure(
            () =>
            {
                byte[] nonCanonical = Encoding.ASCII.GetBytes(
                    "{\"api_secret\":\"test-api-secret\",\"api_key\":\"test-api-key\"}");
                try
                {
                    using BybitCredentialMaterial _ =
                        BybitCredentialMaterial.Parse(nonCanonical);
                }
                finally
                {
                    CryptographicOperations.ZeroMemory(nonCanonical);
                }
            },
            "BYBIT credential envelope must reject noncanonical field order");
    }

    private static void TestAuthenticationWireBytes()
    {
        byte[] payload = Encoding.ASCII.GetBytes(
            "{\"api_key\":\"test-api-key\",\"api_secret\":\"test-api-secret\"}");
        using BybitCredentialMaterial credential = BybitCredentialMaterial.Parse(payload);
        CryptographicOperations.ZeroMemory(payload);
        byte[] request = BybitPrivateWebSocketProtocol.BuildAuthenticationRequest(
            credential,
            new DateTimeOffset(2026, 10, 1, 6, 0, 0, TimeSpan.Zero));
        try
        {
            Check(
                Encoding.ASCII.GetString(request)
                    == "{\"op\":\"auth\",\"args\":[\"test-api-key\",1790834401000,\"3951faceef5b4a8e6c4b9040c1f40119a26937a3db0fb0eb082bfbc4bf64debe\"]}",
                "BYBIT auth signed bytes changed");
        }
        finally
        {
            CryptographicOperations.ZeroMemory(request);
        }

        byte[] subscribe = BybitPrivateWebSocketProtocol.BuildSubscriptionRequest();
        byte[] ping = BybitPrivateWebSocketProtocol.BuildHeartbeatRequest();
        try
        {
            Check(
                Encoding.ASCII.GetString(subscribe)
                    == "{\"op\":\"subscribe\",\"args\":[\"execution\"]}",
                "BYBIT execution subscription bytes changed");
            Check(
                Encoding.ASCII.GetString(ping) == "{\"op\":\"ping\"}",
                "BYBIT heartbeat bytes changed");
        }
        finally
        {
            CryptographicOperations.ZeroMemory(subscribe);
            CryptographicOperations.ZeroMemory(ping);
        }
    }

    private static void TestRouteBinding()
    {
        BybitPrivateWebSocketProtocol.RequireRoute(Route("PAPER", "TESTNET"));
        BybitPrivateWebSocketProtocol.RequireRoute(Route("PAPER", "DEMO"));
        BybitPrivateWebSocketProtocol.RequireRoute(Route("LIVE", "MAINNET"));

        ExpectFailure(
            () => BybitPrivateWebSocketProtocol.RequireRoute(
                Route("PAPER", "TESTNET") with
                {
                    Endpoint = "wss://stream.bybit.com/v5/private"
                }),
            "TESTNET must not relabel onto mainnet socket endpoint");

        ExpectFailure(
            () => BybitPrivateWebSocketProtocol.RequireRoute(
                Route("PAPER", "DEMO") with
                {
                    QualificationId = string.Empty
                }),
            "missing current Q identity must fail before socket use");

        ExpectFailure(
            () => BybitPrivateWebSocketProtocol.RequireRoute(
                Route("PAPER", "DEMO") with
                {
                    NetworkPolicyIdentity = "sha256:" + new string('A', 64)
                }),
            "noncanonical network policy identity must fail before socket use");
    }

    private static void TestControlAcknowledgements()
    {
        byte[] auth = Encoding.UTF8.GetBytes(
            "{\"success\":true,\"ret_msg\":\"\",\"op\":\"auth\",\"conn_id\":\"conn-1\"}");
        BybitAuthenticationAcknowledgement parsed =
            BybitPrivateWebSocketProtocol.ParseAuthenticationAcknowledgement(auth);
        Check(
            parsed.ConnectionId == "conn-1",
            "BYBIT auth connection id changed");

        byte[] subscribe = Encoding.UTF8.GetBytes(
            "{\"success\":true,\"ret_msg\":\"\",\"op\":\"subscribe\",\"conn_id\":\"conn-1\"}");
        BybitSubscriptionAcknowledgement accepted =
            BybitPrivateWebSocketProtocol.ParseSubscriptionAcknowledgement(
                subscribe,
                "conn-1");
        Check(
            accepted.ConnectionId == "conn-1",
            "BYBIT subscription connection id changed");

        ExpectFailure(
            () => BybitPrivateWebSocketProtocol.ParseSubscriptionAcknowledgement(
                subscribe,
                "conn-2"),
            "subscription ack must not cross connection identity");

        byte[] extra = Encoding.UTF8.GetBytes(
            "{\"success\":true,\"ret_msg\":\"\",\"op\":\"auth\",\"conn_id\":\"conn-1\",\"extra\":true}");
        ExpectFailure(
            () => BybitPrivateWebSocketProtocol.ParseAuthenticationAcknowledgement(extra),
            "unknown auth response fields must fail closed");

        CryptographicOperations.ZeroMemory(auth);
        CryptographicOperations.ZeroMemory(subscribe);
        CryptographicOperations.ZeroMemory(extra);
    }

    private static void TestExecutionFrameShape()
    {
        byte[] valid = Encoding.UTF8.GetBytes(
            "{\"topic\":\"execution\",\"id\":\"message-1\",\"creationTime\":1746270400355,\"data\":[{\"category\":\"linear\",\"symbol\":\"BTCUSDT\"}]}");
        try
        {
            BybitPrivateWebSocketProtocol.RequirePrivateExecutionFrame(valid);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(valid);
        }

        ExpectFrameFailure(
            "{\"op\":\"subscribe\",\"success\":true,\"ret_msg\":\"\",\"conn_id\":\"conn-1\"}",
            "control acknowledgement must not receive provider-origin execution receipt");
        ExpectFrameFailure(
            "{\"topic\":\"order\",\"data\":[{}]}",
            "non-execution private topic must not receive execution receipt");
        ExpectFrameFailure(
            "{\"topic\":\"execution\",\"data\":[]}",
            "empty execution data must fail before issuer");
        ExpectFrameFailure(
            "{\"topic\":\"execution\",\"data\":{\"category\":\"linear\"}}",
            "non-array execution data must fail before issuer");
        ExpectFrameFailure(
            "{\"topic\":\"execution\",\"data\":[\"forged\"]}",
            "non-object execution entry must fail before issuer");
        ExpectFrameFailure(
            "{\"topic\":\"execution\",\"topic\":\"order\",\"data\":[{}]}",
            "duplicate topic keys must fail before issuer");
    }

    private static void TestFailClosedBridges()
    {
        ProviderConnectionRequest request = new(
            "BYBIT",
            "account-1",
            "PAPER",
            "TESTNET",
            "execution",
            "credential-handle-1");
        IProviderCurrentAuthorityBoundary current =
            new UnavailableProviderCurrentAuthorityBoundary();
        ExpectFailure(
            () => current.RequireCurrent(request),
            "absent durable current C+Q bridge must fail closed");

        IProviderCredentialMaterialSource credentials =
            new UnavailableProviderCredentialMaterialSource();
        ExpectFailure(
            () => credentials.Resolve(
                Route("PAPER", "TESTNET"),
                "credential-handle-1",
                "READ"),
            "absent canonical provider credential bridge must fail closed");
    }

    private static void TestCallerCannotSelectTerminalAuthority()
    {
        Check(
            !typeof(BybitPrivateWebSocketClient).IsPublic,
            "provider socket implementation must remain inside AutoTrade.Host");
        Check(
            !typeof(IProviderCurrentAuthorityBoundary).IsPublic,
            "current provider authority bridge must remain process-internal");
        Check(
            !typeof(IProviderCredentialMaterialSource).IsPublic,
            "provider credential material bridge must remain process-internal");

        PropertyInfo[] requestProperties =
            typeof(ProviderConnectionRequest).GetProperties();
        HashSet<string> propertyNames = new(
            requestProperties.Select(item => item.Name),
            StringComparer.Ordinal);
        foreach (string prohibited in new[]
        {
            "Endpoint",
            "CapabilityId",
            "QualificationId",
            "QualificationBuildId",
            "NetworkPolicyIdentity",
            "SubscriptionIdentity",
            "TransportIdentity",
        })
        {
            Check(
                !propertyNames.Contains(prohibited),
                "socket request must not accept caller-selected " + prohibited);
        }

        MethodInfo connect = typeof(BybitPrivateWebSocketClient).GetMethod(
            nameof(BybitPrivateWebSocketClient.ConnectAsync),
            BindingFlags.Public | BindingFlags.Static)
            ?? throw new InvalidOperationException(
                "provider socket ConnectAsync is missing");
        Check(
            !connect.GetParameters().Any(item =>
                item.ParameterType == typeof(Uri)
                || item.ParameterType == typeof(string)),
            "socket ConnectAsync must not accept caller URL/text authority");
        Check(
            typeof(BybitPrivateWebSocketClient).GetConstructors(
                BindingFlags.Public | BindingFlags.Instance).Length == 0,
            "provider socket must not expose a public constructor");
    }

    private static void TestFrameLifetime()
    {
        byte[] frame = Encoding.UTF8.GetBytes("{\"topic\":\"execution\"}");
        ProviderPrivateFrameReceipt receipt = new(
            Schema: "autotrade-provider-private-frame-receipt:v1",
            IssuerSessionIdentity: "issuer",
            ConnectionBindingSha256: "sha256:" + new string('a', 64),
            ConnectionIdentity: "connection",
            ConnectionGeneration: 1,
            ReceiveOrdinal: 1,
            FrameSha256: "sha256:" + new string('b', 64),
            FrameLength: frame.Length,
            ObservedAtUtc: "2026-10-01T06:00:00.0000000Z",
            PreviousReceiptSha256: string.Empty,
            ReceiptSha256: "sha256:" + new string('c', 64),
            SignatureBase64: "AA==");
        using (ProviderOriginPrivateFrame owned =
            new ProviderOriginPrivateFrame(frame, receipt))
        {
            Check(
                owned.FrameBytes.SequenceEqual(frame),
                "provider-origin frame must expose exact retained bytes while owned");
        }
        Check(
            frame.All(item => item == 0),
            "provider-origin frame bytes must be zeroed on dispose");
    }

    private static ProviderCurrentRouteAuthority Route(
        string runtime,
        string providerEnvironment)
    {
        string endpoint = (runtime, providerEnvironment) switch
        {
            ("LIVE", "MAINNET") => "wss://stream.bybit.com/v5/private",
            ("PAPER", "TESTNET") => "wss://stream-testnet.bybit.com/v5/private",
            ("PAPER", "DEMO") => "wss://stream-demo.bybit.com/v5/private",
            _ => "wss://invalid.example/v5/private",
        };
        return new ProviderCurrentRouteAuthority(
            ProviderId: "BYBIT",
            AccountId: "account-1",
            RuntimeEnvironment: runtime,
            ProviderEnvironment: providerEnvironment,
            Endpoint: endpoint,
            TopicId: "execution",
            SubscriptionIdentity: "private-execution-v1",
            CapabilityId: "capability-1",
            QualificationId: "qualification-1",
            QualificationBuildId: "bybit-v5-build-1",
            NetworkPolicyIdentity: "sha256:" + new string('a', 64),
            TransportIdentity: "bybit-v5-private-websocket:v1");
    }

    private static void ExpectFrameFailure(string json, string message)
    {
        byte[] payload = Encoding.UTF8.GetBytes(json);
        try
        {
            ExpectFailure(
                () => BybitPrivateWebSocketProtocol.RequirePrivateExecutionFrame(payload),
                message);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(payload);
        }
    }

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
