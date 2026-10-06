using System.Runtime.CompilerServices;
using AutoTrade.Host;

internal static class ProviderAuthenticatedReadDurabilityBoundaryContractTests
{
    [ModuleInitializer]
    internal static void Run()
    {
        DateTimeOffset started =
            new(2026, 10, 6, 21, 0, 0, TimeSpan.Zero);
        using ProviderIssuerAuthority issuer =
            ProviderIssuerAuthority.CreateProcessAuthority(started);
        ProviderAuthenticatedReadSubject subject = new(
            "BYBIT",
            "acct-1",
            "entity-1",
            "PAPER",
            "TESTNET",
            "/v5/account/wallet-balance",
            "AUTHENTICATED_READ",
            "ACCOUNT_READ",
            "ACCOUNT_BALANCES",
            "instrument-version-1",
            "sha256:" + new string('1', 64),
            "sha256:" + new string('2', 64),
            "credential-handle-1",
            7,
            "capability-1",
            "qualification-1",
            "qualification-build-1",
            "adapter-build-1",
            "sha256:" + new string('3', 64),
            "provider-transport:https-v1");
        ProviderAuthenticatedReadAttemptBinding attempt =
            issuer.IssueAuthenticatedReadAttempt(
                subject,
                started.AddMilliseconds(100));
        Dictionary<string, string> query =
            new(StringComparer.Ordinal)
            {
                ["category"] = "linear",
                ["symbol"] = "BTCUSDT",
            };
        ProviderAuthenticatedReadPreparedEvidence evidence =
            new(issuer.Session, attempt, query);

        // Source dictionary mutation after capture cannot rewrite retained evidence.
        query["symbol"] = "ETHUSDT";
        Check(
            evidence.Query["symbol"] == "BTCUSDT",
            "Prepared evidence retained a caller-mutable query alias");

        IProviderAuthenticatedReadDurabilityBoundary unavailable =
            new UnavailableProviderAuthenticatedReadDurabilityBoundary();
        ExpectFailure(
            () => unavailable.CommitPrepared(evidence),
            "missing canonical JournalStore bridge must block Prepared");

        byte[] response =
            System.Text.Encoding.UTF8.GetBytes("{\"retCode\":0}");
        ProviderAuthenticatedReadReceipt providerReceipt =
            issuer.IssueAuthenticatedReadReceipt(
                attempt,
                200,
                response,
                started.AddMilliseconds(200));
        ProviderAuthenticatedReadDurabilityReceipt preparedReceipt =
            new(
                issuer.Session.SessionIdentity,
                attempt.ReadAttemptId,
                attempt.BindingSha256,
                attempt.Subject.QueryDigest,
                "sha256:" + new string('4', 64),
                attempt.ReadAttemptId + ":prepared",
                1,
                "2026-10-06T21:00:00.1500000Z");
        ProviderAuthenticatedReadObservedDurabilityReceipt observedReceipt =
            new(
                issuer.Session.SessionIdentity,
                attempt.ReadAttemptId,
                attempt.BindingSha256,
                providerReceipt.ReceiptSha256,
                providerReceipt.ResponseSha256,
                providerReceipt.HttpStatus,
                providerReceipt.ObservedAtUtc,
                preparedReceipt.JournalIdentity,
                preparedReceipt.ReceiptIdentity,
                preparedReceipt.PreparedEventId,
                preparedReceipt.JournalSequence,
                attempt.ReadAttemptId + ":observed",
                2,
                "2026-10-06T21:00:00.2500000Z");

        ProviderAuthenticatedReadDurabilityVerifier.RequireObservedMatches(
            evidence,
            providerReceipt,
            preparedReceipt,
            observedReceipt,
            response);

        ExpectFailure(
            () => unavailable.CommitObserved(
                evidence,
                providerReceipt,
                preparedReceipt,
                response),
            "missing canonical JournalStore bridge must block Observed");

        ExpectFailure(
            () => ProviderAuthenticatedReadDurabilityVerifier.RequireObservedMatches(
                evidence,
                providerReceipt,
                preparedReceipt,
                new ProviderAuthenticatedReadObservedDurabilityReceipt(
                    issuer.Session.SessionIdentity,
                    attempt.ReadAttemptId,
                    attempt.BindingSha256,
                    providerReceipt.ReceiptSha256,
                    providerReceipt.ResponseSha256,
                    providerReceipt.HttpStatus,
                    providerReceipt.ObservedAtUtc,
                    "sha256:" + new string('5', 64),
                    preparedReceipt.ReceiptIdentity,
                    preparedReceipt.PreparedEventId,
                    preparedReceipt.JournalSequence,
                    attempt.ReadAttemptId + ":observed",
                    2,
                    "2026-10-06T21:00:00.2500000Z"),
                response),
            "Observed durability from another journal must fail closed");

        Dictionary<string, string> oversized = new(StringComparer.Ordinal);
        for (int i = 0; i < 257; i++)
        {
            oversized["k" + i.ToString(System.Globalization.CultureInfo.InvariantCulture)] = "v";
        }
        ExpectFailure(
            () => _ = new ProviderAuthenticatedReadPreparedEvidence(
                issuer.Session,
                issuer.IssueAuthenticatedReadAttempt(
                    subject,
                    started.AddMilliseconds(300)),
                oversized),
            "Prepared query resource bound must reject more than 256 items");
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
