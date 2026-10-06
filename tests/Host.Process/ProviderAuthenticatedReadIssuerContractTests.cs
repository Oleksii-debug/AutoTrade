using System.Runtime.CompilerServices;
using System.Text;
using AutoTrade.Host;

internal static class ProviderAuthenticatedReadIssuerContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        DateTimeOffset started =
            new(2026, 10, 6, 20, 50, 0, TimeSpan.Zero);
        using ProviderIssuerAuthority issuer =
            ProviderIssuerAuthority.CreateProcessAuthority(started);

        ProviderAuthenticatedReadSubject subject = Subject("TESTNET", 11);
        ProviderAuthenticatedReadAttemptBinding attempt =
            issuer.IssueAuthenticatedReadAttempt(
                subject,
                started.AddSeconds(1));

        ProviderIssuerVerifier.RequireValidReadAttempt(
            issuer.Session,
            attempt,
            issuer.Session.SessionIdentity,
            issuer.Session.PublicKeySha256);

        Check(
            attempt.Schema ==
                "autotrade-provider-authenticated-read-attempt:v1",
            "authenticated read attempt schema drifted");
        Check(
            attempt.ReadGeneration == 1,
            "first exact authenticated read scope must use generation 1");
        Check(
            attempt.ReadAttemptId.StartsWith(
                "provider-read:",
                StringComparison.Ordinal),
            "authenticated read attempt id must be Host-issued");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadAttempt(
                issuer.Session,
                attempt with
                {
                    Subject = subject with { ProviderEnvironment = "DEMO" }
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed read attempt must not permit provider-domain relabeling");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadAttempt(
                issuer.Session,
                attempt with
                {
                    Subject = subject with
                    {
                        QueryDigest = "sha256:" + new string('b', 64)
                    }
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed read attempt must not permit query relabeling");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadAttempt(
                issuer.Session,
                attempt with
                {
                    Subject = subject with
                    {
                        QualificationId = "qualification-2"
                    }
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed read attempt must not permit accepted-Q relabeling");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadAttempt(
                issuer.Session,
                attempt with
                {
                    Subject = subject with
                    {
                        EndpointRuleIdentity =
                            "sha256:" + new string('4', 64)
                    }
                },
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed read attempt must not permit endpoint-rule relabeling");

        byte[] response =
            Encoding.UTF8.GetBytes(
                "{\"retCode\":0,\"result\":{\"equity\":\"100.25\"}}");
        ProviderAuthenticatedReadReceipt receipt =
            issuer.IssueAuthenticatedReadReceipt(
                attempt,
                200,
                response,
                started.AddSeconds(2));

        ProviderIssuerVerifier.RequireValidReadReceipt(
            issuer.Session,
            attempt,
            receipt,
            response,
            issuer.Session.SessionIdentity,
            issuer.Session.PublicKeySha256);

        Check(
            receipt.Schema ==
                "autotrade-provider-authenticated-read-receipt:v1",
            "authenticated read receipt schema drifted");
        Check(
            receipt.ReadAttemptBindingSha256 == attempt.BindingSha256,
            "authenticated read receipt lost prepared-attempt identity");
        Check(
            receipt.ResponseLength == response.LongLength,
            "authenticated read receipt lost exact response length");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadReceipt(
                issuer.Session,
                attempt,
                receipt with { HttpStatus = 201 },
                response,
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed read receipt must not permit HTTP status relabeling");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadReceipt(
                issuer.Session,
                attempt,
                receipt,
                Encoding.UTF8.GetBytes("{\"retCode\":0}"),
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "signed read receipt must not authenticate different bytes");

        ExpectFailure(
            () => issuer.IssueAuthenticatedReadReceipt(
                attempt,
                200,
                response,
                started.AddSeconds(3)),
            "one prepared read attempt must issue at most one definitive response receipt");

        ProviderAuthenticatedReadAttemptBinding second =
            issuer.IssueAuthenticatedReadAttempt(
                subject,
                started.AddSeconds(4));
        Check(
            second.ReadGeneration == 2,
            "same exact read scope must advance generation");

        ExpectFailure(
            () => issuer.IssueAuthenticatedReadReceipt(
                second,
                200,
                response,
                started.AddSeconds(3)),
            "authenticated read receipt cannot precede prepared chronology");

        issuer.AbandonAuthenticatedReadAttempt(second);
        ExpectFailure(
            () => issuer.IssueAuthenticatedReadReceipt(
                second,
                200,
                response,
                started.AddSeconds(5)),
            "abandoned authenticated read attempt must remain closed");

        ProviderAuthenticatedReadSubject demoSubject =
            subject with { ProviderEnvironment = "DEMO" };
        ProviderAuthenticatedReadAttemptBinding demo =
            issuer.IssueAuthenticatedReadAttempt(
                demoSubject,
                started.AddSeconds(5));
        Check(
            demo.BindingSha256 != attempt.BindingSha256,
            "TESTNET and DEMO authenticated-read identities must never alias");

        ExpectFailure(
            () => issuer.IssueAuthenticatedReadAttempt(
                subject with { RuntimeEnvironment = "SIMULATION" },
                started.AddSeconds(6)),
            "production provider-origin issuer must reject SIMULATION runtime");

        ExpectFailure(
            () => issuer.IssueAuthenticatedReadAttempt(
                subject with { ProviderId = "bybit" },
                started.AddSeconds(6)),
            "provider identity must be canonical uppercase text");

        ExpectFailure(
            () => issuer.IssueAuthenticatedReadAttempt(
                subject with { Endpoint = "https://api-testnet.bybit.com/v5/account/wallet-balance" },
                started.AddSeconds(6)),
            "issuer must reject caller absolute endpoints");

        ExpectFailure(
            () => issuer.IssueAuthenticatedReadReceipt(
                demo,
                200,
                Array.Empty<byte>(),
                started.AddSeconds(7)),
            "authenticated read issuer must reject empty definitive response bytes");

        issuer.AbandonAuthenticatedReadAttempt(demo);

        using ProviderIssuerAuthority otherIssuer =
            ProviderIssuerAuthority.CreateProcessAuthority(started);
        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadAttempt(
                otherIssuer.Session,
                attempt,
                otherIssuer.Session.SessionIdentity,
                otherIssuer.Session.PublicKeySha256),
            "read attempt from another Host issuer session must not verify");

        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadAttempt(
                issuer.Session,
                attempt,
                otherIssuer.Session.SessionIdentity,
                otherIssuer.Session.PublicKeySha256),
            "caller-selected different issuer pin must fail closed");

        ProviderAuthenticatedReadAttemptBinding forged = attempt with
        {
            BindingSha256 = "sha256:" + new string('a', 64),
            SignatureBase64 = Convert.ToBase64String(new byte[64])
        };
        ExpectFailure(
            () => ProviderIssuerVerifier.RequireValidReadAttempt(
                issuer.Session,
                forged,
                issuer.Session.SessionIdentity,
                issuer.Session.PublicKeySha256),
            "caller-constructed read authority must not verify without Host signature");
    }

    private static ProviderAuthenticatedReadSubject Subject(
        string providerEnvironment,
        long credentialGeneration)
    {
        return new ProviderAuthenticatedReadSubject(
            "BYBIT",
            "acct-1",
            "entity-1",
            "PAPER",
            providerEnvironment,
            "/v5/account/wallet-balance",
            "AUTHENTICATED_READ",
            "ACCOUNT_READ",
            "ACCOUNT_BALANCES",
            "instrument-version-1",
            "sha256:" + new string('1', 64),
            "sha256:" + new string('2', 64),
            "credential-handle-1",
            credentialGeneration,
            "capability-1",
            "qualification-1",
            "qualification-build-1",
            "adapter-build-1",
            "sha256:" + new string('3', 64),
            "provider-transport:https-v1");
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
