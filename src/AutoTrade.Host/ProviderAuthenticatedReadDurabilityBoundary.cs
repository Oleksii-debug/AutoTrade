using System.Collections.ObjectModel;

namespace AutoTrade.Host;

internal sealed class ProviderAuthenticatedReadPreparedEvidence
{
    private readonly IReadOnlyDictionary<string, string> _query;

    internal ProviderAuthenticatedReadPreparedEvidence(
        ProviderIssuerSession issuerSession,
        ProviderAuthenticatedReadAttemptBinding attempt,
        Dictionary<string, string> query)
    {
        ArgumentNullException.ThrowIfNull(issuerSession);
        ArgumentNullException.ThrowIfNull(attempt);
        ArgumentNullException.ThrowIfNull(query);
        if (query.GetType() != typeof(Dictionary<string, string>))
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated-read Prepared query must be an exact inert Dictionary");
        }
        if (query.Count > 256)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated-read Prepared query exceeds the bounded item count");
        }

        ProviderIssuerVerifier.RequireValidReadAttempt(
            issuerSession,
            attempt,
            issuerSession.SessionIdentity,
            issuerSession.PublicKeySha256);
        Dictionary<string, string> copy =
            new(query.Count, StringComparer.Ordinal);
        foreach ((string key, string value) in query)
        {
            if (string.IsNullOrEmpty(key)
                || !string.Equals(key, key.Trim(), StringComparison.Ordinal)
                || key.Length > 512
                || value is null
                || !string.Equals(value, value.Trim(), StringComparison.Ordinal)
                || value.Length > 4096)
            {
                throw new ProviderIssuerAuthorityException(
                    "authenticated-read Prepared query contains non-canonical or oversized text");
            }
            if (!copy.TryAdd(key, value))
            {
                throw new ProviderIssuerAuthorityException(
                    "authenticated-read Prepared query contains duplicate keys");
            }
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

internal sealed class ProviderAuthenticatedReadObservedDurabilityReceipt
{
    internal const string SchemaName =
        "autotrade-provider-read-durable-observed:v1";

    internal ProviderAuthenticatedReadObservedDurabilityReceipt(
        string issuerSessionIdentity,
        string readAttemptId,
        string readAttemptBindingSha256,
        string providerReceiptSha256,
        string responseSha256,
        int httpStatus,
        string observedAtUtc,
        string journalIdentity,
        string preparedReceiptIdentity,
        string preparedEventId,
        long preparedJournalSequence,
        string observedEventId,
        long observedJournalSequence,
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
            providerReceiptSha256,
            nameof(providerReceiptSha256));
        ProviderIssuerAuthority.RequireSha256(
            responseSha256,
            nameof(responseSha256));
        if (httpStatus < 100 || httpStatus > 599)
        {
            throw new ProviderIssuerAuthorityException(
                "durable Observed HTTP status must be 100..599");
        }
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            observedAtUtc,
            nameof(observedAtUtc));
        ProviderIssuerAuthority.RequireSha256(
            journalIdentity,
            nameof(journalIdentity));
        ProviderIssuerAuthority.ExactText(
            preparedReceiptIdentity,
            nameof(preparedReceiptIdentity));
        ProviderIssuerAuthority.ExactText(
            preparedEventId,
            nameof(preparedEventId));
        ProviderIssuerAuthority.ExactText(
            observedEventId,
            nameof(observedEventId));
        if (!string.Equals(
                preparedEventId,
                readAttemptId + ":prepared",
                StringComparison.Ordinal)
            || !string.Equals(
                observedEventId,
                readAttemptId + ":observed",
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "durable Observed event identity does not match read attempt");
        }
        if (preparedJournalSequence <= 0
            || observedJournalSequence <= preparedJournalSequence)
        {
            throw new ProviderIssuerAuthorityException(
                "durable Observed journal chronology is invalid");
        }
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            committedAtUtc,
            nameof(committedAtUtc));
        if (StringComparer.Ordinal.Compare(committedAtUtc, observedAtUtc) < 0)
        {
            throw new ProviderIssuerAuthorityException(
                "durable Observed commit cannot precede provider observation");
        }

        Schema = SchemaName;
        IssuerSessionIdentity = issuerSessionIdentity;
        ReadAttemptId = readAttemptId;
        ReadAttemptBindingSha256 = readAttemptBindingSha256;
        ProviderReceiptSha256 = providerReceiptSha256;
        ResponseSha256 = responseSha256;
        HttpStatus = httpStatus;
        ObservedAtUtc = observedAtUtc;
        JournalIdentity = journalIdentity;
        PreparedReceiptIdentity = preparedReceiptIdentity;
        PreparedEventId = preparedEventId;
        PreparedJournalSequence = preparedJournalSequence;
        ObservedEventId = observedEventId;
        ObservedJournalSequence = observedJournalSequence;
        CommittedAtUtc = committedAtUtc;
        ReceiptIdentity = ProviderIssuerAuthority.ContentIdentity(
            "provider-read-durable-observed",
            ProviderIssuerAuthority.CanonicalMaterial(
                Schema,
                IssuerSessionIdentity,
                ReadAttemptId,
                ReadAttemptBindingSha256,
                ProviderReceiptSha256,
                ResponseSha256,
                HttpStatus.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                ObservedAtUtc,
                JournalIdentity,
                PreparedReceiptIdentity,
                PreparedEventId,
                PreparedJournalSequence.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                ObservedEventId,
                ObservedJournalSequence.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                CommittedAtUtc));
    }

    internal string Schema { get; }
    internal string IssuerSessionIdentity { get; }
    internal string ReadAttemptId { get; }
    internal string ReadAttemptBindingSha256 { get; }
    internal string ProviderReceiptSha256 { get; }
    internal string ResponseSha256 { get; }
    internal int HttpStatus { get; }
    internal string ObservedAtUtc { get; }
    internal string JournalIdentity { get; }
    internal string PreparedReceiptIdentity { get; }
    internal string PreparedEventId { get; }
    internal long PreparedJournalSequence { get; }
    internal string ObservedEventId { get; }
    internal long ObservedJournalSequence { get; }
    internal string CommittedAtUtc { get; }
    internal string ReceiptIdentity { get; }
}

internal interface IProviderAuthenticatedReadDurabilityBoundary
{
    ProviderAuthenticatedReadDurabilityReceipt CommitPrepared(
        ProviderAuthenticatedReadPreparedEvidence evidence);

    ProviderAuthenticatedReadObservedDurabilityReceipt CommitObserved(
        ProviderAuthenticatedReadPreparedEvidence evidence,
        ProviderAuthenticatedReadReceipt providerReceipt,
        ProviderAuthenticatedReadDurabilityReceipt preparedReceipt,
        byte[] responseBytes);
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

    public ProviderAuthenticatedReadObservedDurabilityReceipt CommitObserved(
        ProviderAuthenticatedReadPreparedEvidence evidence,
        ProviderAuthenticatedReadReceipt providerReceipt,
        ProviderAuthenticatedReadDurabilityReceipt preparedReceipt,
        byte[] responseBytes)
    {
        ArgumentNullException.ThrowIfNull(evidence);
        ArgumentNullException.ThrowIfNull(providerReceipt);
        ArgumentNullException.ThrowIfNull(preparedReceipt);
        ArgumentNullException.ThrowIfNull(responseBytes);
        throw new ProviderIssuerAuthorityException(
            "durable authenticated provider-read Observed authority is unavailable in AutoTrade.Host");
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

    internal static void RequireObservedMatches(
        ProviderAuthenticatedReadPreparedEvidence evidence,
        ProviderAuthenticatedReadReceipt providerReceipt,
        ProviderAuthenticatedReadDurabilityReceipt preparedReceipt,
        ProviderAuthenticatedReadObservedDurabilityReceipt observedReceipt,
        byte[] responseBytes)
    {
        ArgumentNullException.ThrowIfNull(evidence);
        ArgumentNullException.ThrowIfNull(providerReceipt);
        ArgumentNullException.ThrowIfNull(preparedReceipt);
        ArgumentNullException.ThrowIfNull(observedReceipt);
        ArgumentNullException.ThrowIfNull(responseBytes);

        RequireMatches(evidence, preparedReceipt);
        ProviderIssuerVerifier.RequireValidReadReceipt(
            evidence.IssuerSession,
            evidence.Attempt,
            providerReceipt,
            responseBytes,
            evidence.IssuerSession.SessionIdentity,
            evidence.IssuerSession.PublicKeySha256);

        if (!string.Equals(
                observedReceipt.Schema,
                ProviderAuthenticatedReadObservedDurabilityReceipt.SchemaName,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.IssuerSessionIdentity,
                evidence.IssuerSession.SessionIdentity,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.ReadAttemptId,
                evidence.Attempt.ReadAttemptId,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.ReadAttemptBindingSha256,
                evidence.Attempt.BindingSha256,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.ProviderReceiptSha256,
                providerReceipt.ReceiptSha256,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.ResponseSha256,
                providerReceipt.ResponseSha256,
                StringComparison.Ordinal)
            || observedReceipt.HttpStatus != providerReceipt.HttpStatus
            || !string.Equals(
                observedReceipt.ObservedAtUtc,
                providerReceipt.ObservedAtUtc,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.JournalIdentity,
                preparedReceipt.JournalIdentity,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.PreparedReceiptIdentity,
                preparedReceipt.ReceiptIdentity,
                StringComparison.Ordinal)
            || !string.Equals(
                observedReceipt.PreparedEventId,
                preparedReceipt.PreparedEventId,
                StringComparison.Ordinal)
            || observedReceipt.PreparedJournalSequence
                != preparedReceipt.JournalSequence
            || !string.Equals(
                observedReceipt.ObservedEventId,
                evidence.Attempt.ReadAttemptId + ":observed",
                StringComparison.Ordinal)
            || observedReceipt.ObservedJournalSequence
                <= preparedReceipt.JournalSequence
            || StringComparer.Ordinal.Compare(
                observedReceipt.CommittedAtUtc,
                providerReceipt.ObservedAtUtc) < 0)
        {
            throw new ProviderIssuerAuthorityException(
                "durable authenticated-read Observed receipt does not match signed Host response");
        }

        string expectedIdentity = ProviderIssuerAuthority.ContentIdentity(
            "provider-read-durable-observed",
            ProviderIssuerAuthority.CanonicalMaterial(
                observedReceipt.Schema,
                observedReceipt.IssuerSessionIdentity,
                observedReceipt.ReadAttemptId,
                observedReceipt.ReadAttemptBindingSha256,
                observedReceipt.ProviderReceiptSha256,
                observedReceipt.ResponseSha256,
                observedReceipt.HttpStatus.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                observedReceipt.ObservedAtUtc,
                observedReceipt.JournalIdentity,
                observedReceipt.PreparedReceiptIdentity,
                observedReceipt.PreparedEventId,
                observedReceipt.PreparedJournalSequence.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                observedReceipt.ObservedEventId,
                observedReceipt.ObservedJournalSequence.ToString(
                    System.Globalization.CultureInfo.InvariantCulture),
                observedReceipt.CommittedAtUtc));
        if (!string.Equals(
                observedReceipt.ReceiptIdentity,
                expectedIdentity,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "durable authenticated-read Observed receipt identity is invalid");
        }
    }
}
