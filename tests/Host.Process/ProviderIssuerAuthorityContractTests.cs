using System.Reflection;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using AutoTrade.Host;

internal static class ProviderIssuerAuthorityContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        DateTimeOffset started =
            new(2026, 10, 1, 5, 0, 0, TimeSpan.Zero);
        using ProviderIssuerAuthority issuer =
            ProviderIssuerAuthority.CreateProcessAuthority(started);

        Check(
            !typeof(ProviderIssuerAuthority).IsPublic,
            "production signing issuer type must not be exported by AutoTrade.Host");

        Check(
            !typeof(ProviderIssuerVerifier).IsPublic,
            "provider issuer verifier must remain inside the Host trust boundary");

        ProviderConnectionSubject subject =
            Subject("TESTNET", 7);
        byte[] auth =
            Encoding.UTF8.GetBytes("authenticated-handshake-v1");
        byte[] subscription =
            Encoding.UTF8.GetBytes("subscription-ack-v1");

        ProviderAuthenticatedConnectionBinding connection =
            issuer.IssueAuthenticatedConnection(
                subject,
                auth,
                subscription,
                started.AddSeconds(1));

        VerifyConnection(issuer, connection);
        Check(
            connection.Schema == "autotrade-provider-connection-binding:v2",
            "issuer/verifier connection schema must remain v2");
        Check(
            connection.ConnectionGeneration == 1,
            "first exact scope connection must use generation 1");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidConnectionBinding(
                issuer.Session,
                connection with
                {
                    Schema = "autotrade-provider-connection-binding:v1"
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "legacy v1 connection schema must not be accepted as v2 authority");
        Check(
            connection.ConnectionIdentity.StartsWith(
                "provider-connection:",
                StringComparison.Ordinal),
            "connection identity must be Host-issued");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidConnectionBinding(
                issuer.Session,
                connection with
                {
                    Subject = subject with
                    {
                        ProviderEnvironment = "DEMO"
                    }
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed connection must not permit provider-domain relabeling");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidConnectionBinding(
                issuer.Session,
                connection with
                {
                    Subject = subject with
                    {
                        QualificationId = "qualification-2"
                    }
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed connection must not permit accepted-Q relabeling");

        byte[] frameOne =
            Encoding.UTF8.GetBytes("provider-frame-one");
        ProviderPrivateFrameReceipt receiptOne =
            issuer.IssuePrivateFrameReceipt(
                connection,
                frameOne,
                started.AddSeconds(2));
        VerifyFrame(issuer, connection, receiptOne, frameOne);
        Check(
            receiptOne.ReceiveOrdinal == 1,
            "first exact frame must use receive ordinal 1");
        Check(
            receiptOne.PreviousReceiptSha256 == string.Empty,
            "first exact frame must not fabricate a predecessor");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidFrameReceipt(
                issuer.Session,
                connection,
                receiptOne with
                {
                    FrameSha256 =
                        "sha256:" + new string('b', 64)
                },
                frameOne,
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed receipt must not permit frame-digest relabeling");

        byte[] frameTwo =
            Encoding.UTF8.GetBytes("provider-frame-two");
        ProviderPrivateFrameReceipt receiptTwo =
            issuer.IssuePrivateFrameReceipt(
                connection,
                frameTwo,
                started.AddSeconds(3));
        VerifyFrame(issuer, connection, receiptTwo, frameTwo);
        Check(
            receiptTwo.ReceiveOrdinal == 2,
            "second exact frame must advance receive ordinal");
        Check(
            receiptTwo.PreviousReceiptSha256 ==
                receiptOne.ReceiptSha256,
            "frame receipt chain must bind prior receipt");

        ExpectFailure(
            () => VerifyFrame(
                issuer,
                connection,
                receiptOne,
                Encoding.UTF8.GetBytes("tampered")),
            "copied receipt must not authenticate different bytes");

        ProviderAuthenticatedConnectionBinding reconnect =
            issuer.IssueAuthenticatedConnection(
                subject,
                auth,
                subscription,
                started.AddSeconds(4));
        Check(
            reconnect.ConnectionGeneration == 2,
            "reconnect in the same scope must advance generation");
        Check(
            reconnect.ConnectionIdentity !=
                connection.ConnectionIdentity,
            "reconnect must receive a new Host-issued identity");

        ExpectFailure(
            () => VerifyFrame(
                issuer,
                reconnect,
                receiptOne,
                frameOne),
            "generation-1 receipt must not relabel onto generation 2");

        ProviderAuthenticatedConnectionBinding demo =
            issuer.IssueAuthenticatedConnection(
                Subject("DEMO", 7),
                auth,
                subscription,
                started.AddSeconds(5));
        Check(
            demo.BindingSha256 != connection.BindingSha256,
            "TESTNET and DEMO must not share connection identity");

        ProviderAuthenticatedConnectionBinding rotated =
            issuer.IssueAuthenticatedConnection(
                Subject("TESTNET", 8),
                auth,
                subscription,
                started.AddSeconds(6));
        Check(
            rotated.BindingSha256 != connection.BindingSha256,
            "credential rotation must change connection binding");

        using ProviderIssuerAuthority attacker =
            ProviderIssuerAuthority.CreateProcessAuthority(started);
        ProviderAuthenticatedConnectionBinding forgedConnection =
            attacker.IssueAuthenticatedConnection(
                subject,
                auth,
                subscription,
                started.AddSeconds(1));
        ProviderPrivateFrameReceipt forgedReceipt =
            attacker.IssuePrivateFrameReceipt(
                forgedConnection,
                frameOne,
                started.AddSeconds(2));

        ExpectFailure(
            () => ProviderIssuerVerifier.RequirePinnedSession(
                attacker.Session,
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "self-issued attacker must not satisfy pinned Host session");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidConnectionBinding(
                attacker.Session,
                forgedConnection,
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "attacker binding must fail original Host pin");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidFrameReceipt(
                attacker.Session,
                forgedConnection,
                forgedReceipt,
                frameOne,
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "attacker frame must fail original Host pin");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequirePinnedSession(
                issuer.Session with
                {
                    StartedAtUtc = "2026-10-01T05:00:00Z"
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "noncanonical issuer UTC text must fail closed");

        MethodInfo issueConnection =
            typeof(ProviderIssuerAuthority).GetMethod(
                nameof(
                    ProviderIssuerAuthority
                        .IssueAuthenticatedConnection))
            ?? throw new InvalidOperationException(
                "issuer connection method is missing");

        string[] parameterNames =
            issueConnection
                .GetParameters()
                .Select(item => item.Name ?? string.Empty)
                .ToArray();

        Check(
            !parameterNames.Contains(
                "connectionId",
                StringComparer.OrdinalIgnoreCase),
            "public issuer must not accept caller connection id");
        Check(
            !parameterNames.Contains(
                "connectionGeneration",
                StringComparer.OrdinalIgnoreCase),
            "public issuer must not accept caller connection generation");

        Check(
            !typeof(ProviderIssuerAuthority)
                .GetMethods(
                    BindingFlags.Public |
                    BindingFlags.Instance |
                    BindingFlags.Static)
                .Any(item =>
                    item.Name.Contains(
                        "Sign",
                        StringComparison.OrdinalIgnoreCase) ||
                    typeof(AsymmetricAlgorithm)
                        .IsAssignableFrom(item.ReturnType)),
            "public issuer must not expose signing primitive/key");

        Check(
            !typeof(ProviderIssuerAuthority)
                .GetProperties(
                    BindingFlags.Public |
                    BindingFlags.Instance |
                    BindingFlags.Static)
                .Any(item =>
                    typeof(AsymmetricAlgorithm)
                        .IsAssignableFrom(item.PropertyType)),
            "public issuer properties must not expose signing key");

        ExpectFailure(
            () => issuer.IssueAuthenticatedConnection(
                subject with
                {
                    RuntimeEnvironment = "SIMULATION"
                },
                auth,
                subscription,
                started.AddSeconds(7)),
            "issuer must not mint financial evidence for SIMULATION");

        ExpectFailure(
            () => issuer.IssueAuthenticatedConnection(
                subject with
                {
                    CredentialGeneration = 0
                },
                auth,
                subscription,
                started.AddSeconds(7)),
            "zero credential generation must fail closed");

        ExpectFailure(
            () => issuer.IssueAuthenticatedConnection(
                subject with
                {
                    CredentialPurpose = "WRITE"
                },
                auth,
                subscription,
                started.AddSeconds(7)),
            "unsupported credential purpose must fail closed");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidConnectionBinding(
                issuer.Session,
                connection with
                {
                    Subject = subject with
                    {
                        CredentialPurpose = "TRADE"
                    }
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed connection must not permit credential-purpose relabeling");

        ExpectFailure(
            () => issuer.IssueAuthenticatedConnection(
                subject,
                Array.Empty<byte>(),
                subscription,
                started.AddSeconds(7)),
            "missing auth transcript must fail closed");

        ExpectFailure(
            () => issuer.IssueAuthenticatedConnection(
                subject,
                auth,
                Array.Empty<byte>(),
                started.AddSeconds(7)),
            "missing subscription acknowledgement must fail closed");

        ProviderIssuerAuthority disposed =
            ProviderIssuerAuthority.CreateProcessAuthority(started);
        disposed.Dispose();
        bool disposedRejected = false;
        try
        {
            disposed.IssueAuthenticatedConnection(
                subject,
                auth,
                subscription,
                started.AddSeconds(8));
        }
        catch (ObjectDisposedException)
        {
            disposedRejected = true;
        }
        Check(
            disposedRejected,
            "disposed Host issuer must not issue new evidence");
    }

    private static ProviderConnectionSubject Subject(
        string providerEnvironment,
        long credentialGeneration)
    {
        return new ProviderConnectionSubject(
            ProviderId: "BYBIT",
            AccountId: "account-1",
            RuntimeEnvironment: "PAPER",
            ProviderEnvironment: providerEnvironment,
            Endpoint: "wss://stream.bybit.com/v5/private",
            TopicId: "execution",
            SubscriptionIdentity: "private-execution-v1",
            CredentialHandleId: "credential-handle-1",
            CredentialGeneration: credentialGeneration,
            CredentialPurpose: "READ",
            CapabilityId: "capability-1",
            QualificationId: "qualification-1",
            QualificationBuildId: "bybit-v5-build-1",
            NetworkPolicyIdentity:
                "sha256:" + new string('a', 64),
            TransportIdentity:
                "bybit-v5-private-websocket:v1");
    }

    private static void VerifyConnection(
        ProviderIssuerAuthority issuer,
        ProviderAuthenticatedConnectionBinding connection)
    {
        ProviderIssuerVerifier.RequireValidConnectionBinding(
            issuer.Session,
            connection,
            issuer.Session.SessionIdentity,
            issuer.Session.PublicKeySha256);
    }

    private static void VerifyFrame(
        ProviderIssuerAuthority issuer,
        ProviderAuthenticatedConnectionBinding connection,
        ProviderPrivateFrameReceipt receipt,
        byte[] frameBytes)
    {
        ProviderIssuerVerifier.RequireValidFrameReceipt(
            issuer.Session,
            connection,
            receipt,
            frameBytes,
            issuer.Session.SessionIdentity,
            issuer.Session.PublicKeySha256);
    }

    private static void Check(
        bool condition,
        string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }

    private static void ExpectFailure(
        Action action,
        string message)
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
