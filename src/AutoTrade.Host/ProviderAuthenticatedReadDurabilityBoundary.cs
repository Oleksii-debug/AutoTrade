using System.Collections.ObjectModel;

namespace AutoTrade.Host;

internal sealed class ProviderAuthenticatedReadPreparedEvidence
{
    private readonly IReadOnlyDictionary<string, string> _query;

    internal ProviderAuthenticatedReadPreparedEvidence(
        ProviderIssuerSession issuerSession,
        ProviderAuthenticatedReadAttemptBinding attempt,
        IReadOnlyDictionary<string, string> query)
    {
        ArgumentNullException.ThrowIfNull(issuerSession);
        ArgumentNullException.ThrowIfNull(attempt);
        ArgumentNullException.ThrowIfNull(query);

        ProviderIssuerVerifier.RequireValidReadAttempt(
            issuerSession,
            attempt,
            issuerSession.SessionIdentity,
            issuerSession.PublicKeySha256);
        string canonicalQuery =
            BybitAuthenticatedReadClient.CanonicalQuery(query);
        Dictionary<string, string> copy =
            new(query.Count, StringComparer.Ordinal);
        foreach ((string key, string value) in query)
        {
            copy.Add(key, value);
        }
        if (!string.Equals(
                BybitAuthenticatedReadClient.CanonicalQuery(copy),
                canonicalQuery,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated-read Prepared query changed during evidence capture");
        }

        IssuerSession = issuerSession;
        Attempt = attempt;
        _query = new ReadOnlyDictionary<string, string>(copy);
    }

    internal ProviderIssuerSession IssuerSession { get; }
    internal ProviderAuthenticatedReadAttemptBinding Attempt { get; }
    internal IReadOnlyDictionary<string, string> Query => _query;
}

internal sealed class ProviderAuthenticatedReadDurabilityReceipt
{
    internal const string SchemaName =
        "autotrade-provider-read-durable-prepared:v1";

    internal ProviderAuthenticatedReadDurabilityReceipt(
        string issuerSessionIdentity,
        string readAttemptId,
        string readAttemptBindingSha256,
        string queryDigest,
        string journalIdentity,
        string preparedEventId,
        long journalSequence,
        string committedAtUtc)
    {
        ProviderIssuerAuthority.ExactText(
            issuerSessionIdentity,
            nameof(issuerSessionIdentity));
        ProviderIssuerAuthority.ExactText(
            readAttemptId,
            nameof(readAttemptId));
        ProviderIssuerAuthority.RequireSha256(
            readAttemptBindingSha256,
            nameof(readAttemptBindingSha256));
        ProviderIssuerAuthority.RequireSha256(
            queryDigest,
            nameof(queryDigest));
        ProviderIssuerAuthority.RequireSha256(
            journalIdentity,
            nameof(journalIdentity));
        ProviderIssuerAuthority.ExactText(
            preparedEventId,
            nameof(preparedEventId));
        if (journalSequence <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "durable Prepared journal sequence must be positive");
        }
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            committedAtUtc,
            nameof(committedAtUtc));

        Schema = SchemaName;
        IssuerSessionIdentity = issuerSessionIdentity;
        ReadAttemptId = readAttemptId;
        ReadAttemptBindingSha256 = readAttemptBindingSha256;
        QueryDigest = queryDigest;
        JournalIdentity = journalIdentity;
        PreparedEventId = preparedEventId;
        JournalSequence = journalSequence;
        CommittedAtUtc = committedAtUtc;
        ReceiptIdentity = ProviderIssuerAuthority.ContentIdentity(
            "provider-read-durable-prepared",
            ProviderIssuerAuthority.CanonicalMaterial(
                Schema,
                IssuerSessionIdentity,
                ReadAttemptId,
                ReadAttemptBindingSha256,
                QueryDigest,
                JournalIdentity,
                PreparedEventId,
                JournalSequence.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                CommittedAtUtc));
    }

    internal string Schema { get; }
    internal string IssuerSessionIdentity { get; }
    internal string ReadAttemptId { get; }
    internal string ReadAttemptBindingSha256 { get; }
    internal string QueryDigest { get; }
    internal string JournalIdentity { get; }
    internal string PreparedEventId { get; }
    internal long JournalSequence { get; }
    internal string CommittedAtUtc { get; }
    internal string ReceiptIdentity { get; }
}

internal interface IProviderAuthenticatedReadDurabilityBoundary
{
    ProviderAuthenticatedReadDurabilityReceipt CommitPrepared(
        ProviderAuthenticatedReadPreparedEvidence evidence);
}

internal sealed class UnavailableProviderAuthenticatedReadDurabilityBoundary
    : IProviderAuthenticatedReadDurabilityBoundary
{
    public ProviderAuthenticatedReadDurabilityReceipt CommitPrepared(
        ProviderAuthenticatedReadPreparedEvidence evidence)
    {
        ArgumentNullException.ThrowIfNull(evidence);
        throw new ProviderIssuerAuthorityException(
            "durable authenticated provider-read Prepared authority is unavailable in AutoTrade.Host");
    }
}

internal static class ProviderAuthenticatedReadDurabilityVerifier
{
    internal static void RequireMatches(
        ProviderAuthenticatedReadPreparedEvidence evidence,
        ProviderAuthenticatedReadDurabilityReceipt receipt)
    {
        ArgumentNullException.ThrowIfNull(evidence);
        ArgumentNullException.ThrowIfNull(receipt);

        ProviderAuthenticatedReadAttemptBinding attempt = evidence.Attempt;
        if (!string.Equals(
                receipt.Schema,
                ProviderAuthenticatedReadDurabilityReceipt.SchemaName,
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.IssuerSessionIdentity,
                evidence.IssuerSession.SessionIdentity,
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.ReadAttemptId,
                attempt.ReadAttemptId,
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.ReadAttemptBindingSha256,
                attempt.BindingSha256,
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.QueryDigest,
                attempt.Subject.QueryDigest,
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.PreparedEventId,
                attempt.ReadAttemptId + ":prepared",
                StringComparison.Ordinal)
            || StringComparer.Ordinal.Compare(
                receipt.CommittedAtUtc,
                attempt.PreparedAtUtc) < 0
            || receipt.JournalSequence <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "durable authenticated-read Prepared receipt does not match signed Host attempt");
        }

        string expectedIdentity = ProviderIssuerAuthority.ContentIdentity(
            "provider-read-durable-prepared",
            ProviderIssuerAuthority.CanonicalMaterial(
                receipt.Schema,
                receipt.IssuerSessionIdentity,
                receipt.ReadAttemptId,
                receipt.ReadAttemptBindingSha256,
                receipt.QueryDigest,
                receipt.JournalIdentity,
                receipt.PreparedEventId,
                receipt.JournalSequence.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                receipt.CommittedAtUtc));
        if (!string.Equals(
                receipt.ReceiptIdentity,
                expectedIdentity,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "durable authenticated-read Prepared receipt identity is invalid");
        }
    }
}
