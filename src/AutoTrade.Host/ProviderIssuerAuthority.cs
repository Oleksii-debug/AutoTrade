using System.Buffers.Binary;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace AutoTrade.Host;

internal sealed class ProviderIssuerAuthorityException : InvalidOperationException
{
    public ProviderIssuerAuthorityException(string message) : base(message) { }
}

public sealed record ProviderIssuerSession(
    string Schema,
    string IssuerInstanceId,
    string StartedAtUtc,
    string PublicKeySpkiBase64,
    string PublicKeySha256,
    string SessionIdentity);

public sealed record ProviderConnectionSubject(
    string ProviderId,
    string AccountId,
    string RuntimeEnvironment,
    string ProviderEnvironment,
    string Endpoint,
    string TopicId,
    string SubscriptionIdentity,
    string CredentialHandleId,
    long CredentialGeneration,
    string CredentialPurpose,
    string CapabilityId,
    string QualificationId,
    string QualificationBuildId,
    string NetworkPolicyIdentity,
    string TransportIdentity);

public sealed record ProviderAuthenticatedConnectionBinding(
    string Schema,
    string IssuerSessionIdentity,
    ProviderConnectionSubject Subject,
    string AuthenticationTranscriptSha256,
    string SubscriptionAcknowledgementSha256,
    long ConnectionGeneration,
    string ConnectionIdentity,
    string OpenedAtUtc,
    string BindingSha256,
    string SignatureBase64);

public sealed record ProviderPrivateFrameReceipt(
    string Schema,
    string IssuerSessionIdentity,
    string ConnectionBindingSha256,
    string ConnectionIdentity,
    long ConnectionGeneration,
    long ReceiveOrdinal,
    string FrameSha256,
    long FrameLength,
    string ObservedAtUtc,
    string PreviousReceiptSha256,
    string ReceiptSha256,
    string SignatureBase64);


public sealed record ProviderAuthenticatedReadSubject(
    string ProviderId,
    string AccountId,
    string EntityId,
    string RuntimeEnvironment,
    string ProviderEnvironment,
    string Endpoint,
    string Surface,
    string PermissionScope,
    string DataEntitlement,
    string InstrumentVersion,
    string QueryDigest,
    string EndpointRuleIdentity,
    string CredentialHandleId,
    long CredentialGeneration,
    string CapabilityId,
    string QualificationId,
    string QualificationBuildId,
    string AdapterBuildIdentity,
    string NetworkPolicyIdentity,
    string TransportIdentity);

public sealed record ProviderAuthenticatedReadAttemptBinding(
    string Schema,
    string IssuerSessionIdentity,
    ProviderAuthenticatedReadSubject Subject,
    long ReadGeneration,
    string ReadAttemptId,
    string PreparedAtUtc,
    string BindingSha256,
    string SignatureBase64);

public sealed record ProviderAuthenticatedReadReceipt(
    string Schema,
    string IssuerSessionIdentity,
    string ReadAttemptBindingSha256,
    string ReadAttemptId,
    long ReadGeneration,
    int HttpStatus,
    string ResponseSha256,
    long ResponseLength,
    string ObservedAtUtc,
    string ReceiptSha256,
    string SignatureBase64);

public sealed record HostSenderFenceChallenge(
    string BackupManifestSha256,
    string OwnerScope,
    string ProviderId,
    string ProviderEnvironment,
    string CredentialHandleId,
    long CredentialGeneration,
    string OldOwnerId,
    long OldOwnerEpoch,
    string NewOwnerId,
    long NewOwnerEpoch);

public sealed record HostSenderFenceReceipt(
    string Schema,
    string IssuerSessionIdentity,
    string FenceMethod,
    string LeaseScopeId,
    string LeaseOwnerRecordSha256,
    string LeaseAcquiredAtUtc,
    string OwnerScope,
    string ProviderId,
    string AccountId,
    string RuntimeEnvironment,
    string ProviderEnvironment,
    string CredentialHandleId,
    long CredentialGeneration,
    string BackupManifestSha256,
    string OldOwnerId,
    long OldOwnerEpoch,
    string NewOwnerId,
    long NewOwnerEpoch,
    string FencedAtUtc,
    string ReceiptSha256,
    string SignatureBase64);

/// <summary>
/// Process-bound evidence issuer for authenticated provider sessions.
/// It deliberately does not open sockets, resolve credentials, decide risk, or grant
/// PAPER/LIVE authority. AutoTrade.Host must call it only after its canonical
/// credential-bearing authentication/subscription path succeeds. The private signing
/// key never leaves this process and no public method signs caller-selected material.
/// </summary>
internal sealed class ProviderIssuerAuthority : IDisposable
{
    internal const string SessionSchema = "autotrade-provider-issuer-session:v1";
    internal const string ConnectionSchema = "autotrade-provider-connection-binding:v2";
    internal const string FrameSchema = "autotrade-provider-private-frame-receipt:v1";
    internal const string ReadAttemptSchema = "autotrade-provider-authenticated-read-attempt:v1";
    internal const string ReadReceiptSchema = "autotrade-provider-authenticated-read-receipt:v1";
    internal const string HostSenderFenceSchema = "autotrade-host-sender-fence:v1";
    internal const string HostSenderFenceMethod = "SAME_HOST_EXCLUSIVE_LEASE_HANDOFF";
    private static readonly HashSet<string> FinancialRuntimes =
        new(StringComparer.Ordinal) { "PAPER", "LIVE" };

    private readonly object _gate = new();
    private readonly ECDsa _signingKey;
    private readonly Dictionary<string, long> _connectionGenerations = new(StringComparer.Ordinal);
    private readonly Dictionary<string, (long Ordinal, string PreviousReceiptSha256)> _frameHeads =
        new(StringComparer.Ordinal);
    private readonly Dictionary<string, long> _readGenerations = new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _openReadAttempts = new(StringComparer.Ordinal);
    private bool _disposed;

    private ProviderIssuerAuthority(ECDsa signingKey, ProviderIssuerSession session)
    {
        _signingKey = signingKey;
        Session = session;
    }

    public ProviderIssuerSession Session { get; }

    public static ProviderIssuerAuthority CreateProcessAuthority(DateTimeOffset startedAtUtc)
    {
        string startedAt = CanonicalUtc(startedAtUtc, nameof(startedAtUtc));
        ECDsa key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        try
        {
            byte[] publicKey = key.ExportSubjectPublicKeyInfo();
            string publicKeyBase64 = Convert.ToBase64String(publicKey);
            string publicKeySha256 = Sha256(publicKey);
            string issuerInstanceId = "provider-issuer:" + Guid.NewGuid().ToString("N");
            string sessionIdentity = ContentIdentity(
                "provider-issuer-session",
                CanonicalMaterial(
                    SessionSchema,
                    issuerInstanceId,
                    startedAt,
                    publicKeyBase64,
                    publicKeySha256));
            ProviderIssuerSession session = new(
                SessionSchema,
                issuerInstanceId,
                startedAt,
                publicKeyBase64,
                publicKeySha256,
                sessionIdentity);
            return new ProviderIssuerAuthority(key, session);
        }
        catch
        {
            key.Dispose();
            throw;
        }
    }

    public ProviderAuthenticatedConnectionBinding IssueAuthenticatedConnection(
        ProviderConnectionSubject subject,
        byte[] authenticationTranscript,
        byte[] subscriptionAcknowledgement,
        DateTimeOffset openedAtUtc)
    {
        ArgumentNullException.ThrowIfNull(subject);
        ArgumentNullException.ThrowIfNull(authenticationTranscript);
        ArgumentNullException.ThrowIfNull(subscriptionAcknowledgement);
        RequireSubject(subject);
        RequireEvidenceBytes(authenticationTranscript, nameof(authenticationTranscript));
        RequireEvidenceBytes(subscriptionAcknowledgement, nameof(subscriptionAcknowledgement));
        string openedAt = CanonicalUtc(openedAtUtc, nameof(openedAtUtc));
        string authenticationSha256 = Sha256(authenticationTranscript);
        string subscriptionSha256 = Sha256(subscriptionAcknowledgement);

        lock (_gate)
        {
            ThrowIfDisposed();
            string scope = ConnectionScope(subject);
            long generation = _connectionGenerations.TryGetValue(scope, out long current)
                ? checked(current + 1)
                : 1;
            _connectionGenerations[scope] = generation;
            string connectionIdentity = "provider-connection:" + Guid.NewGuid().ToString("N");
            byte[] material = ConnectionMaterial(
                Session.SessionIdentity,
                subject,
                authenticationSha256,
                subscriptionSha256,
                generation,
                connectionIdentity,
                openedAt);
            string bindingSha256 = Sha256(material);
            string signature = Sign(material);
            _frameHeads.Add(connectionIdentity, (0, string.Empty));
            return new ProviderAuthenticatedConnectionBinding(
                ConnectionSchema,
                Session.SessionIdentity,
                subject,
                authenticationSha256,
                subscriptionSha256,
                generation,
                connectionIdentity,
                openedAt,
                bindingSha256,
                signature);
        }
    }

    public ProviderPrivateFrameReceipt IssuePrivateFrameReceipt(
        ProviderAuthenticatedConnectionBinding connection,
        byte[] frameBytes,
        DateTimeOffset observedAtUtc)
    {
        ArgumentNullException.ThrowIfNull(connection);
        ArgumentNullException.ThrowIfNull(frameBytes);
        RequireEvidenceBytes(frameBytes, nameof(frameBytes));
        string observedAt = CanonicalUtc(observedAtUtc, nameof(observedAtUtc));

        lock (_gate)
        {
            ThrowIfDisposed();
            ProviderIssuerVerifier.RequireValidConnectionBinding(
                Session,
                connection,
                Session.SessionIdentity,
                Session.PublicKeySha256);
            if (!_frameHeads.TryGetValue(connection.ConnectionIdentity, out var head))
            {
                throw new ProviderIssuerAuthorityException(
                    "connection binding was not issued by this live Host issuer instance");
            }

            long ordinal = checked(head.Ordinal + 1);
            string frameSha256 = Sha256(frameBytes);
            byte[] material = FrameMaterial(
                Session.SessionIdentity,
                connection.BindingSha256,
                connection.ConnectionIdentity,
                connection.ConnectionGeneration,
                ordinal,
                frameSha256,
                frameBytes.LongLength,
                observedAt,
                head.PreviousReceiptSha256);
            string receiptSha256 = Sha256(material);
            string signature = Sign(material);
            _frameHeads[connection.ConnectionIdentity] = (ordinal, receiptSha256);
            return new ProviderPrivateFrameReceipt(
                FrameSchema,
                Session.SessionIdentity,
                connection.BindingSha256,
                connection.ConnectionIdentity,
                connection.ConnectionGeneration,
                ordinal,
                frameSha256,
                frameBytes.LongLength,
                observedAt,
                head.PreviousReceiptSha256,
                receiptSha256,
                signature);
        }
    }

    public ProviderAuthenticatedReadAttemptBinding IssueAuthenticatedReadAttempt(
        ProviderAuthenticatedReadSubject subject,
        DateTimeOffset preparedAtUtc)
    {
        ArgumentNullException.ThrowIfNull(subject);
        RequireReadSubject(subject);
        string preparedAt = CanonicalUtc(preparedAtUtc, nameof(preparedAtUtc));

        lock (_gate)
        {
            ThrowIfDisposed();
            string scope = ReadScope(subject);
            long generation = _readGenerations.TryGetValue(scope, out long current)
                ? checked(current + 1)
                : 1;
            _readGenerations[scope] = generation;
            string readAttemptId = "provider-read:" + Guid.NewGuid().ToString("N");
            byte[] material = ReadAttemptMaterial(
                Session.SessionIdentity,
                subject,
                generation,
                readAttemptId,
                preparedAt);
            string bindingSha256 = Sha256(material);
            string signature = Sign(material);
            _openReadAttempts.Add(readAttemptId, bindingSha256);
            return new ProviderAuthenticatedReadAttemptBinding(
                ReadAttemptSchema,
                Session.SessionIdentity,
                subject,
                generation,
                readAttemptId,
                preparedAt,
                bindingSha256,
                signature);
        }
    }

    public ProviderAuthenticatedReadReceipt IssueAuthenticatedReadReceipt(
        ProviderAuthenticatedReadAttemptBinding attempt,
        int httpStatus,
        byte[] responseBytes,
        DateTimeOffset observedAtUtc)
    {
        ArgumentNullException.ThrowIfNull(attempt);
        ArgumentNullException.ThrowIfNull(responseBytes);
        if (httpStatus < 100 || httpStatus > 599)
        {
            throw new ProviderIssuerAuthorityException(
                "httpStatus must be an exact HTTP status in the 100..599 range");
        }
        RequireEvidenceBytes(responseBytes, nameof(responseBytes));
        string observedAt = CanonicalUtc(observedAtUtc, nameof(observedAtUtc));

        lock (_gate)
        {
            ThrowIfDisposed();
            ProviderIssuerVerifier.RequireValidReadAttempt(
                Session,
                attempt,
                Session.SessionIdentity,
                Session.PublicKeySha256);
            if (!_openReadAttempts.TryGetValue(
                    attempt.ReadAttemptId,
                    out string? issuedBindingSha256) ||
                !string.Equals(
                    issuedBindingSha256,
                    attempt.BindingSha256,
                    StringComparison.Ordinal))
            {
                throw new ProviderIssuerAuthorityException(
                    "authenticated read attempt was not issued by this live Host issuer instance");
            }

            DateTimeOffset prepared = DateTimeOffset.ParseExact(
                attempt.PreparedAtUtc,
                "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
            DateTimeOffset observed = DateTimeOffset.ParseExact(
                observedAt,
                "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
            if (observed < prepared)
            {
                throw new ProviderIssuerAuthorityException(
                    "authenticated read response cannot precede the prepared attempt");
            }

            string responseSha256 = Sha256(responseBytes);
            byte[] material = ReadReceiptMaterial(
                Session.SessionIdentity,
                attempt.BindingSha256,
                attempt.ReadAttemptId,
                attempt.ReadGeneration,
                httpStatus,
                responseSha256,
                responseBytes.LongLength,
                observedAt);
            string receiptSha256 = Sha256(material);
            string signature = Sign(material);
            _openReadAttempts.Remove(attempt.ReadAttemptId);
            return new ProviderAuthenticatedReadReceipt(
                ReadReceiptSchema,
                Session.SessionIdentity,
                attempt.BindingSha256,
                attempt.ReadAttemptId,
                attempt.ReadGeneration,
                httpStatus,
                responseSha256,
                responseBytes.LongLength,
                observedAt,
                receiptSha256,
                signature);
        }
    }

    public void AbandonAuthenticatedReadAttempt(
        ProviderAuthenticatedReadAttemptBinding attempt)
    {
        ArgumentNullException.ThrowIfNull(attempt);
        lock (_gate)
        {
            ThrowIfDisposed();
            ProviderIssuerVerifier.RequireValidReadAttempt(
                Session,
                attempt,
                Session.SessionIdentity,
                Session.PublicKeySha256);
            if (!_openReadAttempts.TryGetValue(
                    attempt.ReadAttemptId,
                    out string? issuedBindingSha256) ||
                !string.Equals(
                    issuedBindingSha256,
                    attempt.BindingSha256,
                    StringComparison.Ordinal))
            {
                throw new ProviderIssuerAuthorityException(
                    "authenticated read attempt was not issued by this live Host issuer instance");
            }
            _openReadAttempts.Remove(attempt.ReadAttemptId);
        }
    }

    public HostSenderFenceReceipt IssueHostSenderFence(
        HostLifetimeExclusiveLease lease,
        HostSenderFenceChallenge challenge,
        DateTimeOffset fencedAtUtc)
    {
        ArgumentNullException.ThrowIfNull(lease);
        ArgumentNullException.ThrowIfNull(challenge);
        lease.RequireHeld();
        RequireHostSenderFenceChallenge(lease, challenge);
        string fencedAt = CanonicalUtc(fencedAtUtc, nameof(fencedAtUtc));
        RequireCanonicalUtcText(lease.AcquiredAtUtc, nameof(lease.AcquiredAtUtc));
        if (string.CompareOrdinal(fencedAt, lease.AcquiredAtUtc) < 0)
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence receipt cannot predate exclusive lease acquisition");
        }

        lock (_gate)
        {
            ThrowIfDisposed();
            lease.RequireHeld();
            byte[] material = HostSenderFenceMaterial(
                Session.SessionIdentity,
                HostSenderFenceMethod,
                lease.ScopeId,
                lease.OwnerRecordSha256,
                lease.AcquiredAtUtc,
                challenge.OwnerScope,
                challenge.ProviderId,
                lease.Options.AccountId,
                lease.Options.Environment,
                challenge.ProviderEnvironment,
                challenge.CredentialHandleId,
                challenge.CredentialGeneration,
                challenge.BackupManifestSha256,
                challenge.OldOwnerId,
                challenge.OldOwnerEpoch,
                challenge.NewOwnerId,
                challenge.NewOwnerEpoch,
                fencedAt);
            string receiptSha256 = Sha256(material);
            string signature = Sign(material);
            return new HostSenderFenceReceipt(
                HostSenderFenceSchema,
                Session.SessionIdentity,
                HostSenderFenceMethod,
                lease.ScopeId,
                lease.OwnerRecordSha256,
                lease.AcquiredAtUtc,
                challenge.OwnerScope,
                challenge.ProviderId,
                lease.Options.AccountId,
                lease.Options.Environment,
                challenge.ProviderEnvironment,
                challenge.CredentialHandleId,
                challenge.CredentialGeneration,
                challenge.BackupManifestSha256,
                challenge.OldOwnerId,
                challenge.OldOwnerEpoch,
                challenge.NewOwnerId,
                challenge.NewOwnerEpoch,
                fencedAt,
                receiptSha256,
                signature);
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            if (_disposed) return;
            _disposed = true;
            _connectionGenerations.Clear();
            _frameHeads.Clear();
            _readGenerations.Clear();
            _openReadAttempts.Clear();
            _signingKey.Dispose();
        }
        GC.SuppressFinalize(this);
    }

    private string Sign(byte[] material)
    {
        byte[] hash = SHA256.HashData(material);
        return Convert.ToBase64String(
            _signingKey.SignHash(
                hash,
                DSASignatureFormat.IeeeP1363FixedFieldConcatenation));
    }

    private void ThrowIfDisposed()
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(ProviderIssuerAuthority));
        }
    }

    internal static void RequireSubject(ProviderConnectionSubject subject)
    {
        ExactText(subject.ProviderId, nameof(subject.ProviderId));
        ExactText(subject.AccountId, nameof(subject.AccountId));
        ExactText(subject.RuntimeEnvironment, nameof(subject.RuntimeEnvironment));
        if (!FinancialRuntimes.Contains(subject.RuntimeEnvironment))
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer is restricted to PAPER/LIVE runtime environments");
        }
        ExactText(subject.ProviderEnvironment, nameof(subject.ProviderEnvironment));
        ExactText(subject.Endpoint, nameof(subject.Endpoint));
        ExactText(subject.TopicId, nameof(subject.TopicId));
        ExactText(subject.SubscriptionIdentity, nameof(subject.SubscriptionIdentity));
        ExactText(subject.CredentialHandleId, nameof(subject.CredentialHandleId));
        if (subject.CredentialGeneration <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "CredentialGeneration must be positive");
        }
        ExactText(subject.CredentialPurpose, nameof(subject.CredentialPurpose));
        if (subject.CredentialPurpose != "READ" && subject.CredentialPurpose != "TRADE")
        {
            throw new ProviderIssuerAuthorityException(
                "CredentialPurpose must be READ or TRADE");
        }
        ExactText(subject.CapabilityId, nameof(subject.CapabilityId));
        ExactText(subject.QualificationId, nameof(subject.QualificationId));
        ExactText(subject.QualificationBuildId, nameof(subject.QualificationBuildId));
        RequireSha256(subject.NetworkPolicyIdentity, nameof(subject.NetworkPolicyIdentity));
        ExactText(subject.TransportIdentity, nameof(subject.TransportIdentity));
    }

    internal static void RequireReadSubject(ProviderAuthenticatedReadSubject subject)
    {
        ExactText(subject.ProviderId, nameof(subject.ProviderId));
        ExactText(subject.AccountId, nameof(subject.AccountId));
        ExactText(subject.EntityId, nameof(subject.EntityId));
        ExactText(subject.RuntimeEnvironment, nameof(subject.RuntimeEnvironment));
        if (!FinancialRuntimes.Contains(subject.RuntimeEnvironment))
        {
            throw new ProviderIssuerAuthorityException(
                "provider read issuer is restricted to PAPER/LIVE runtime environments");
        }
        ExactText(subject.ProviderEnvironment, nameof(subject.ProviderEnvironment));
        ExactText(subject.Endpoint, nameof(subject.Endpoint));
        ExactText(subject.Surface, nameof(subject.Surface));
        ExactText(subject.PermissionScope, nameof(subject.PermissionScope));
        ExactText(subject.DataEntitlement, nameof(subject.DataEntitlement));
        ExactText(subject.InstrumentVersion, nameof(subject.InstrumentVersion));
        RequireSha256(subject.QueryDigest, nameof(subject.QueryDigest));
        RequireSha256(subject.EndpointRuleIdentity, nameof(subject.EndpointRuleIdentity));
        ExactText(subject.CredentialHandleId, nameof(subject.CredentialHandleId));
        if (subject.CredentialGeneration <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "CredentialGeneration must be positive");
        }
        ExactText(subject.CapabilityId, nameof(subject.CapabilityId));
        ExactText(subject.QualificationId, nameof(subject.QualificationId));
        ExactText(subject.QualificationBuildId, nameof(subject.QualificationBuildId));
        ExactText(subject.AdapterBuildIdentity, nameof(subject.AdapterBuildIdentity));
        RequireSha256(subject.NetworkPolicyIdentity, nameof(subject.NetworkPolicyIdentity));
        ExactText(subject.TransportIdentity, nameof(subject.TransportIdentity));
    }

    private static string ReadScope(ProviderAuthenticatedReadSubject subject)
    {
        return Convert.ToBase64String(
            SHA256.HashData(
                CanonicalMaterial(
                    subject.ProviderId,
                    subject.AccountId,
                    subject.EntityId,
                    subject.RuntimeEnvironment,
                    subject.ProviderEnvironment,
                    subject.Endpoint,
                    subject.Surface,
                    subject.PermissionScope,
                    subject.DataEntitlement,
                    subject.InstrumentVersion,
                    subject.QueryDigest,
                    subject.EndpointRuleIdentity,
                    subject.CredentialHandleId,
                    subject.CredentialGeneration.ToString(CultureInfo.InvariantCulture),
                    subject.CapabilityId,
                    subject.QualificationId,
                    subject.QualificationBuildId,
                    subject.AdapterBuildIdentity,
                    subject.NetworkPolicyIdentity,
                    subject.TransportIdentity)));
    }

    internal static byte[] ReadAttemptMaterial(
        string issuerSessionIdentity,
        ProviderAuthenticatedReadSubject subject,
        long readGeneration,
        string readAttemptId,
        string preparedAtUtc)
    {
        return CanonicalMaterial(
            ReadAttemptSchema,
            issuerSessionIdentity,
            subject.ProviderId,
            subject.AccountId,
            subject.EntityId,
            subject.RuntimeEnvironment,
            subject.ProviderEnvironment,
            subject.Endpoint,
            subject.Surface,
            subject.PermissionScope,
            subject.DataEntitlement,
            subject.InstrumentVersion,
            subject.QueryDigest,
            subject.EndpointRuleIdentity,
            subject.CredentialHandleId,
            subject.CredentialGeneration.ToString(CultureInfo.InvariantCulture),
            subject.CapabilityId,
            subject.QualificationId,
            subject.QualificationBuildId,
            subject.AdapterBuildIdentity,
            subject.NetworkPolicyIdentity,
            subject.TransportIdentity,
            readGeneration.ToString(CultureInfo.InvariantCulture),
            readAttemptId,
            preparedAtUtc);
    }

    internal static byte[] ReadReceiptMaterial(
        string issuerSessionIdentity,
        string readAttemptBindingSha256,
        string readAttemptId,
        long readGeneration,
        int httpStatus,
        string responseSha256,
        long responseLength,
        string observedAtUtc)
    {
        return CanonicalMaterial(
            ReadReceiptSchema,
            issuerSessionIdentity,
            readAttemptBindingSha256,
            readAttemptId,
            readGeneration.ToString(CultureInfo.InvariantCulture),
            httpStatus.ToString(CultureInfo.InvariantCulture),
            responseSha256,
            responseLength.ToString(CultureInfo.InvariantCulture),
            observedAtUtc);
    }

    private static void RequireHostSenderFenceChallenge(
        HostLifetimeExclusiveLease lease,
        HostSenderFenceChallenge challenge)
    {
        lease.RequireHeld();
        RequireSha256(
            challenge.BackupManifestSha256,
            nameof(challenge.BackupManifestSha256));
        ExactText(challenge.OwnerScope, nameof(challenge.OwnerScope));
        ExactText(challenge.ProviderId, nameof(challenge.ProviderId));
        ExactText(
            challenge.ProviderEnvironment,
            nameof(challenge.ProviderEnvironment));
        ExactText(
            challenge.CredentialHandleId,
            nameof(challenge.CredentialHandleId));
        ExactText(challenge.OldOwnerId, nameof(challenge.OldOwnerId));
        ExactText(challenge.NewOwnerId, nameof(challenge.NewOwnerId));

        if (!string.Equals(
                challenge.ProviderId,
                challenge.ProviderId.ToUpperInvariant(),
                StringComparison.Ordinal) ||
            !string.Equals(
                challenge.ProviderEnvironment,
                challenge.ProviderEnvironment.ToUpperInvariant(),
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence provider domain must be canonical uppercase");
        }
        if (!FinancialRuntimes.Contains(lease.Options.Environment))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence is restricted to PAPER/LIVE runtime environments");
        }
        if (challenge.CredentialGeneration <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence credential generation must be positive");
        }
        if (challenge.OldOwnerEpoch <= 0 ||
            challenge.NewOwnerEpoch != checked(challenge.OldOwnerEpoch + 1))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence owner epochs must describe one exact successor transition");
        }
        if (string.Equals(
                challenge.OldOwnerId,
                challenge.NewOwnerId,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence transition must change owner identity");
        }

        string expectedOwnerScope =
            lease.Options.Environment + ":" + lease.Options.AccountId;
        if (!string.Equals(
                challenge.ProviderId,
                lease.Options.Provider,
                StringComparison.Ordinal) ||
            !string.Equals(
                challenge.ProviderEnvironment,
                lease.Options.ProviderEnvironment,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence provider domain does not match the held Host lease");
        }
        if (!string.Equals(
                challenge.OwnerScope,
                expectedOwnerScope,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence owner scope does not match the held Host lease");
        }
        if (!string.Equals(
                challenge.NewOwnerId,
                lease.Options.HostId,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence new owner is not the Host holding the lease");
        }
        if (!string.Equals(
                lease.ScopeId,
                HostLifetimeExclusiveLease.ScopeIdFor(
                    lease.Options.Provider,
                    lease.Options.ProviderEnvironment,
                    lease.Options.Environment,
                    lease.Options.AccountId),
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "sender fence lease scope identity is inconsistent");
        }
        RequireSha256(
            lease.OwnerRecordSha256,
            nameof(lease.OwnerRecordSha256));
    }

    internal static byte[] HostSenderFenceMaterial(
        string issuerSessionIdentity,
        string fenceMethod,
        string leaseScopeId,
        string leaseOwnerRecordSha256,
        string leaseAcquiredAtUtc,
        string ownerScope,
        string providerId,
        string accountId,
        string runtimeEnvironment,
        string providerEnvironment,
        string credentialHandleId,
        long credentialGeneration,
        string backupManifestSha256,
        string oldOwnerId,
        long oldOwnerEpoch,
        string newOwnerId,
        long newOwnerEpoch,
        string fencedAtUtc)
    {
        return CanonicalMaterial(
            HostSenderFenceSchema,
            issuerSessionIdentity,
            fenceMethod,
            leaseScopeId,
            leaseOwnerRecordSha256,
            leaseAcquiredAtUtc,
            ownerScope,
            providerId,
            accountId,
            runtimeEnvironment,
            providerEnvironment,
            credentialHandleId,
            credentialGeneration.ToString(CultureInfo.InvariantCulture),
            backupManifestSha256,
            oldOwnerId,
            oldOwnerEpoch.ToString(CultureInfo.InvariantCulture),
            newOwnerId,
            newOwnerEpoch.ToString(CultureInfo.InvariantCulture),
            fencedAtUtc);
    }

    private static string ConnectionScope(ProviderConnectionSubject subject)
    {
        return Convert.ToBase64String(
            SHA256.HashData(
                CanonicalMaterial(
                    subject.ProviderId,
                    subject.AccountId,
                    subject.RuntimeEnvironment,
                    subject.ProviderEnvironment,
                    subject.Endpoint,
                    subject.TopicId,
                    subject.SubscriptionIdentity)));
    }

    internal static byte[] ConnectionMaterial(
        string issuerSessionIdentity,
        ProviderConnectionSubject subject,
        string authenticationTranscriptSha256,
        string subscriptionAcknowledgementSha256,
        long connectionGeneration,
        string connectionIdentity,
        string openedAtUtc)
    {
        return CanonicalMaterial(
            ConnectionSchema,
            issuerSessionIdentity,
            subject.ProviderId,
            subject.AccountId,
            subject.RuntimeEnvironment,
            subject.ProviderEnvironment,
            subject.Endpoint,
            subject.TopicId,
            subject.SubscriptionIdentity,
            subject.CredentialHandleId,
            subject.CredentialGeneration.ToString(CultureInfo.InvariantCulture),
            subject.CredentialPurpose,
            subject.CapabilityId,
            subject.QualificationId,
            subject.QualificationBuildId,
            subject.NetworkPolicyIdentity,
            subject.TransportIdentity,
            authenticationTranscriptSha256,
            subscriptionAcknowledgementSha256,
            connectionGeneration.ToString(CultureInfo.InvariantCulture),
            connectionIdentity,
            openedAtUtc);
    }

    internal static byte[] FrameMaterial(
        string issuerSessionIdentity,
        string connectionBindingSha256,
        string connectionIdentity,
        long connectionGeneration,
        long receiveOrdinal,
        string frameSha256,
        long frameLength,
        string observedAtUtc,
        string previousReceiptSha256)
    {
        return CanonicalMaterial(
            FrameSchema,
            issuerSessionIdentity,
            connectionBindingSha256,
            connectionIdentity,
            connectionGeneration.ToString(CultureInfo.InvariantCulture),
            receiveOrdinal.ToString(CultureInfo.InvariantCulture),
            frameSha256,
            frameLength.ToString(CultureInfo.InvariantCulture),
            observedAtUtc,
            previousReceiptSha256);
    }

    internal static byte[] CanonicalMaterial(params string[] fields)
    {
        ArgumentNullException.ThrowIfNull(fields);
        using MemoryStream stream = new();
        Span<byte> prefix = stackalloc byte[4];
        BinaryPrimitives.WriteInt32BigEndian(prefix, fields.Length);
        stream.Write(prefix);
        foreach (string field in fields)
        {
            string exact = field ??
                throw new ProviderIssuerAuthorityException("canonical field cannot be null");
            byte[] encoded = Encoding.UTF8.GetBytes(exact);
            BinaryPrimitives.WriteInt32BigEndian(prefix, encoded.Length);
            stream.Write(prefix);
            stream.Write(encoded);
        }
        return stream.ToArray();
    }

    internal static string Sha256(byte[] value)
    {
        return "sha256:" +
            Convert.ToHexString(SHA256.HashData(value)).ToLowerInvariant();
    }

    internal static string ContentIdentity(string prefix, byte[] value)
    {
        return prefix + ":" + Sha256(value);
    }

    internal static string CanonicalUtc(DateTimeOffset value, string name)
    {
        if (value == default)
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be a concrete UTC instant");
        }
        return value.ToUniversalTime().ToString(
            "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            CultureInfo.InvariantCulture);
    }

    internal static void RequireCanonicalUtcText(string value, string name)
    {
        ExactText(value, name);
        if (!DateTimeOffset.TryParseExact(
                value,
                "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                out DateTimeOffset parsed) ||
            !string.Equals(CanonicalUtc(parsed, name), value, StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical UTC text");
        }
    }

    internal static void ExactText(string value, string name)
    {
        if (string.IsNullOrEmpty(value) ||
            !string.Equals(value, value.Trim(), StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical non-empty text");
        }
    }

    internal static void RequireSha256(string value, string name)
    {
        ExactText(value, name);
        if (value.Length != 71 ||
            !value.StartsWith("sha256:", StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical sha256 text");
        }
        for (int index = 7; index < value.Length; index++)
        {
            char character = value[index];
            bool valid =
                (character >= '0' && character <= '9') ||
                (character >= 'a' && character <= 'f');
            if (!valid)
            {
                throw new ProviderIssuerAuthorityException(
                    name + " must be canonical lowercase sha256 text");
            }
        }
    }

    internal static byte[] DecodeCanonicalBase64(string value, string name)
    {
        ExactText(value, name);
        byte[] decoded;
        try
        {
            decoded = Convert.FromBase64String(value);
        }
        catch (FormatException)
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical base64");
        }
        if (!string.Equals(
                Convert.ToBase64String(decoded),
                value,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical base64");
        }
        return decoded;
    }

    private static void RequireEvidenceBytes(byte[] value, string name)
    {
        if (value.Length == 0)
        {
            throw new ProviderIssuerAuthorityException(
                name + " must contain exact retained evidence bytes");
        }
    }
}

internal static class ProviderIssuerVerifier
{
    public static void RequirePinnedSession(
        ProviderIssuerSession session,
        string expectedSessionIdentity,
        string expectedPublicKeySha256)
    {
        ArgumentNullException.ThrowIfNull(session);
        ProviderIssuerAuthority.ExactText(
            expectedSessionIdentity,
            nameof(expectedSessionIdentity));
        ProviderIssuerAuthority.RequireSha256(
            expectedPublicKeySha256,
            nameof(expectedPublicKeySha256));
        RequireSessionSelfIntegrity(session);
        if (!string.Equals(
                session.SessionIdentity,
                expectedSessionIdentity,
                StringComparison.Ordinal) ||
            !string.Equals(
                session.PublicKeySha256,
                expectedPublicKeySha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer session does not match the pinned Host authority");
        }
    }

    public static void RequireValidHostSenderFenceReceipt(
        ProviderIssuerSession session,
        HostSenderFenceReceipt receipt,
        string expectedSessionIdentity,
        string expectedPublicKeySha256)
    {
        ArgumentNullException.ThrowIfNull(session);
        ArgumentNullException.ThrowIfNull(receipt);
        RequirePinnedSession(
            session,
            expectedSessionIdentity,
            expectedPublicKeySha256);

        if (!string.Equals(
                receipt.Schema,
                ProviderIssuerAuthority.HostSenderFenceSchema,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.IssuerSessionIdentity,
                session.SessionIdentity,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.FenceMethod,
                ProviderIssuerAuthority.HostSenderFenceMethod,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Host sender fence schema/issuer/method is invalid");
        }

        ProviderIssuerAuthority.ExactText(
            receipt.LeaseScopeId,
            nameof(receipt.LeaseScopeId));
        ProviderIssuerAuthority.RequireSha256(
            receipt.LeaseOwnerRecordSha256,
            nameof(receipt.LeaseOwnerRecordSha256));
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            receipt.LeaseAcquiredAtUtc,
            nameof(receipt.LeaseAcquiredAtUtc));
        ProviderIssuerAuthority.ExactText(
            receipt.OwnerScope,
            nameof(receipt.OwnerScope));
        ProviderIssuerAuthority.ExactText(
            receipt.ProviderId,
            nameof(receipt.ProviderId));
        ProviderIssuerAuthority.ExactText(
            receipt.AccountId,
            nameof(receipt.AccountId));
        ProviderIssuerAuthority.ExactText(
            receipt.RuntimeEnvironment,
            nameof(receipt.RuntimeEnvironment));
        ProviderIssuerAuthority.ExactText(
            receipt.ProviderEnvironment,
            nameof(receipt.ProviderEnvironment));
        ProviderIssuerAuthority.ExactText(
            receipt.CredentialHandleId,
            nameof(receipt.CredentialHandleId));
        ProviderIssuerAuthority.RequireSha256(
            receipt.BackupManifestSha256,
            nameof(receipt.BackupManifestSha256));
        ProviderIssuerAuthority.ExactText(
            receipt.OldOwnerId,
            nameof(receipt.OldOwnerId));
        ProviderIssuerAuthority.ExactText(
            receipt.NewOwnerId,
            nameof(receipt.NewOwnerId));
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            receipt.FencedAtUtc,
            nameof(receipt.FencedAtUtc));
        ProviderIssuerAuthority.RequireSha256(
            receipt.ReceiptSha256,
            nameof(receipt.ReceiptSha256));

        if (receipt.CredentialGeneration <= 0 ||
            receipt.OldOwnerEpoch <= 0 ||
            receipt.NewOwnerEpoch != checked(receipt.OldOwnerEpoch + 1) ||
            string.Equals(
                receipt.OldOwnerId,
                receipt.NewOwnerId,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Host sender fence transition metadata is invalid");
        }
        if (receipt.RuntimeEnvironment is not ("PAPER" or "LIVE") ||
            !string.Equals(
                receipt.ProviderId,
                receipt.ProviderId.ToUpperInvariant(),
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.ProviderEnvironment,
                receipt.ProviderEnvironment.ToUpperInvariant(),
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Host sender fence financial scope is invalid");
        }
        string expectedScope =
            HostLifetimeExclusiveLease.ScopeIdFor(
                receipt.ProviderId,
                receipt.ProviderEnvironment,
                receipt.RuntimeEnvironment,
                receipt.AccountId);
        if (!string.Equals(
                receipt.LeaseScopeId,
                expectedScope,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.OwnerScope,
                receipt.RuntimeEnvironment + ":" + receipt.AccountId,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Host sender fence lease/owner scope is inconsistent");
        }
        if (string.CompareOrdinal(
                receipt.FencedAtUtc,
                receipt.LeaseAcquiredAtUtc) < 0)
        {
            throw new ProviderIssuerAuthorityException(
                "Host sender fence receipt predates exclusive lease acquisition");
        }

        byte[] material = ProviderIssuerAuthority.HostSenderFenceMaterial(
            receipt.IssuerSessionIdentity,
            receipt.FenceMethod,
            receipt.LeaseScopeId,
            receipt.LeaseOwnerRecordSha256,
            receipt.LeaseAcquiredAtUtc,
            receipt.OwnerScope,
            receipt.ProviderId,
            receipt.AccountId,
            receipt.RuntimeEnvironment,
            receipt.ProviderEnvironment,
            receipt.CredentialHandleId,
            receipt.CredentialGeneration,
            receipt.BackupManifestSha256,
            receipt.OldOwnerId,
            receipt.OldOwnerEpoch,
            receipt.NewOwnerId,
            receipt.NewOwnerEpoch,
            receipt.FencedAtUtc);
        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(material),
                receipt.ReceiptSha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Host sender fence receipt digest does not match exact material");
        }
        RequireSignature(
            session,
            material,
            receipt.SignatureBase64,
            "Host sender fence receipt");
    }

    public static void RequireValidConnectionBinding(
        ProviderIssuerSession session,
        ProviderAuthenticatedConnectionBinding connection,
        string expectedSessionIdentity,
        string expectedPublicKeySha256)
    {
        ArgumentNullException.ThrowIfNull(session);
        ArgumentNullException.ThrowIfNull(connection);
        RequirePinnedSession(
            session,
            expectedSessionIdentity,
            expectedPublicKeySha256);
        RequireValidConnectionBindingAfterPin(session, connection);
    }

    public static void RequireValidFrameReceipt(
        ProviderIssuerSession session,
        ProviderAuthenticatedConnectionBinding connection,
        ProviderPrivateFrameReceipt receipt,
        byte[] frameBytes,
        string expectedSessionIdentity,
        string expectedPublicKeySha256)
    {
        ArgumentNullException.ThrowIfNull(session);
        ArgumentNullException.ThrowIfNull(connection);
        ArgumentNullException.ThrowIfNull(receipt);
        ArgumentNullException.ThrowIfNull(frameBytes);
        RequirePinnedSession(
            session,
            expectedSessionIdentity,
            expectedPublicKeySha256);
        RequireValidConnectionBindingAfterPin(session, connection);

        if (!string.Equals(
                receipt.Schema,
                ProviderIssuerAuthority.FrameSchema,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.IssuerSessionIdentity,
                session.SessionIdentity,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.ConnectionBindingSha256,
                connection.BindingSha256,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.ConnectionIdentity,
                connection.ConnectionIdentity,
                StringComparison.Ordinal) ||
            receipt.ConnectionGeneration != connection.ConnectionGeneration)
        {
            throw new ProviderIssuerAuthorityException(
                "frame receipt is not bound to the exact authenticated connection");
        }

        if (receipt.ReceiveOrdinal <= 0 ||
            receipt.FrameLength != frameBytes.LongLength)
        {
            throw new ProviderIssuerAuthorityException(
                "frame receipt ordinal/length is invalid");
        }

        ProviderIssuerAuthority.RequireCanonicalUtcText(
            receipt.ObservedAtUtc,
            nameof(receipt.ObservedAtUtc));
        ProviderIssuerAuthority.RequireSha256(
            receipt.FrameSha256,
            nameof(receipt.FrameSha256));
        ProviderIssuerAuthority.RequireSha256(
            receipt.ReceiptSha256,
            nameof(receipt.ReceiptSha256));
        if (!string.IsNullOrEmpty(receipt.PreviousReceiptSha256))
        {
            ProviderIssuerAuthority.RequireSha256(
                receipt.PreviousReceiptSha256,
                nameof(receipt.PreviousReceiptSha256));
        }

        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(frameBytes),
                receipt.FrameSha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "frame receipt digest does not match exact held bytes");
        }

        byte[] material = ProviderIssuerAuthority.FrameMaterial(
            receipt.IssuerSessionIdentity,
            receipt.ConnectionBindingSha256,
            receipt.ConnectionIdentity,
            receipt.ConnectionGeneration,
            receipt.ReceiveOrdinal,
            receipt.FrameSha256,
            receipt.FrameLength,
            receipt.ObservedAtUtc,
            receipt.PreviousReceiptSha256);
        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(material),
                receipt.ReceiptSha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "frame receipt digest does not match exact receipt material");
        }
        RequireSignature(
            session,
            material,
            receipt.SignatureBase64,
            "frame receipt");
    }

    public static void RequireValidReadAttempt(
        ProviderIssuerSession session,
        ProviderAuthenticatedReadAttemptBinding attempt,
        string expectedSessionIdentity,
        string expectedPublicKeySha256)
    {
        ArgumentNullException.ThrowIfNull(session);
        ArgumentNullException.ThrowIfNull(attempt);
        RequirePinnedSession(
            session,
            expectedSessionIdentity,
            expectedPublicKeySha256);
        RequireValidReadAttemptAfterPin(session, attempt);
    }

    public static void RequireValidReadReceipt(
        ProviderIssuerSession session,
        ProviderAuthenticatedReadAttemptBinding attempt,
        ProviderAuthenticatedReadReceipt receipt,
        byte[] responseBytes,
        string expectedSessionIdentity,
        string expectedPublicKeySha256)
    {
        ArgumentNullException.ThrowIfNull(session);
        ArgumentNullException.ThrowIfNull(attempt);
        ArgumentNullException.ThrowIfNull(receipt);
        ArgumentNullException.ThrowIfNull(responseBytes);
        RequirePinnedSession(
            session,
            expectedSessionIdentity,
            expectedPublicKeySha256);
        RequireValidReadAttemptAfterPin(session, attempt);

        if (!string.Equals(
                receipt.Schema,
                ProviderIssuerAuthority.ReadReceiptSchema,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.IssuerSessionIdentity,
                session.SessionIdentity,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.ReadAttemptBindingSha256,
                attempt.BindingSha256,
                StringComparison.Ordinal) ||
            !string.Equals(
                receipt.ReadAttemptId,
                attempt.ReadAttemptId,
                StringComparison.Ordinal) ||
            receipt.ReadGeneration != attempt.ReadGeneration)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read receipt is not bound to the exact prepared attempt");
        }
        if (receipt.HttpStatus < 100 || receipt.HttpStatus > 599 ||
            receipt.ResponseLength <= 0 ||
            receipt.ResponseLength != responseBytes.LongLength)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read receipt status/length is invalid");
        }
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            receipt.ObservedAtUtc,
            nameof(receipt.ObservedAtUtc));
        ProviderIssuerAuthority.RequireSha256(
            receipt.ResponseSha256,
            nameof(receipt.ResponseSha256));
        ProviderIssuerAuthority.RequireSha256(
            receipt.ReceiptSha256,
            nameof(receipt.ReceiptSha256));
        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(responseBytes),
                receipt.ResponseSha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read receipt digest does not match exact response bytes");
        }

        DateTimeOffset prepared = DateTimeOffset.ParseExact(
            attempt.PreparedAtUtc,
            "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
        DateTimeOffset observed = DateTimeOffset.ParseExact(
            receipt.ObservedAtUtc,
            "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
            CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
        if (observed < prepared)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read receipt chronology is invalid");
        }

        byte[] material = ProviderIssuerAuthority.ReadReceiptMaterial(
            receipt.IssuerSessionIdentity,
            receipt.ReadAttemptBindingSha256,
            receipt.ReadAttemptId,
            receipt.ReadGeneration,
            receipt.HttpStatus,
            receipt.ResponseSha256,
            receipt.ResponseLength,
            receipt.ObservedAtUtc);
        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(material),
                receipt.ReceiptSha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read receipt digest does not match exact receipt material");
        }
        RequireSignature(
            session,
            material,
            receipt.SignatureBase64,
            "authenticated read receipt");
    }

    private static void RequireValidReadAttemptAfterPin(
        ProviderIssuerSession session,
        ProviderAuthenticatedReadAttemptBinding attempt)
    {
        if (!string.Equals(
                attempt.Schema,
                ProviderIssuerAuthority.ReadAttemptSchema,
                StringComparison.Ordinal) ||
            !string.Equals(
                attempt.IssuerSessionIdentity,
                session.SessionIdentity,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read attempt issuer/session identity is invalid");
        }
        if (attempt.Subject is null)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read subject is missing");
        }
        ProviderIssuerAuthority.RequireReadSubject(attempt.Subject);
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            attempt.PreparedAtUtc,
            nameof(attempt.PreparedAtUtc));
        ProviderIssuerAuthority.ExactText(
            attempt.ReadAttemptId,
            nameof(attempt.ReadAttemptId));
        if (!attempt.ReadAttemptId.StartsWith("provider-read:", StringComparison.Ordinal) ||
            attempt.ReadGeneration <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read attempt identity/generation is invalid");
        }
        ProviderIssuerAuthority.RequireSha256(
            attempt.BindingSha256,
            nameof(attempt.BindingSha256));

        byte[] material = ProviderIssuerAuthority.ReadAttemptMaterial(
            attempt.IssuerSessionIdentity,
            attempt.Subject,
            attempt.ReadGeneration,
            attempt.ReadAttemptId,
            attempt.PreparedAtUtc);
        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(material),
                attempt.BindingSha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read attempt digest does not match exact attempt material");
        }
        RequireSignature(
            session,
            material,
            attempt.SignatureBase64,
            "authenticated read attempt");
    }

    private static void RequireValidConnectionBindingAfterPin(
        ProviderIssuerSession session,
        ProviderAuthenticatedConnectionBinding connection)
    {
        if (!string.Equals(
                connection.Schema,
                ProviderIssuerAuthority.ConnectionSchema,
                StringComparison.Ordinal) ||
            !string.Equals(
                connection.IssuerSessionIdentity,
                session.SessionIdentity,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "connection binding issuer/session identity is invalid");
        }

        if (connection.Subject is null)
        {
            throw new ProviderIssuerAuthorityException(
                "connection subject is missing");
        }
        ProviderIssuerAuthority.RequireSubject(connection.Subject);
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            connection.OpenedAtUtc,
            nameof(connection.OpenedAtUtc));
        ProviderIssuerAuthority.ExactText(
            connection.ConnectionIdentity,
            nameof(connection.ConnectionIdentity));
        if (connection.ConnectionGeneration <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "connection generation must be positive");
        }
        ProviderIssuerAuthority.RequireSha256(
            connection.AuthenticationTranscriptSha256,
            nameof(connection.AuthenticationTranscriptSha256));
        ProviderIssuerAuthority.RequireSha256(
            connection.SubscriptionAcknowledgementSha256,
            nameof(connection.SubscriptionAcknowledgementSha256));
        ProviderIssuerAuthority.RequireSha256(
            connection.BindingSha256,
            nameof(connection.BindingSha256));

        byte[] material = ProviderIssuerAuthority.ConnectionMaterial(
            connection.IssuerSessionIdentity,
            connection.Subject,
            connection.AuthenticationTranscriptSha256,
            connection.SubscriptionAcknowledgementSha256,
            connection.ConnectionGeneration,
            connection.ConnectionIdentity,
            connection.OpenedAtUtc);
        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(material),
                connection.BindingSha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "connection binding digest does not match exact binding material");
        }
        RequireSignature(
            session,
            material,
            connection.SignatureBase64,
            "connection binding");
    }

    private static void RequireSessionSelfIntegrity(
        ProviderIssuerSession session)
    {
        if (!string.Equals(
                session.Schema,
                ProviderIssuerAuthority.SessionSchema,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer session schema is invalid");
        }

        ProviderIssuerAuthority.ExactText(
            session.IssuerInstanceId,
            nameof(session.IssuerInstanceId));
        ProviderIssuerAuthority.RequireCanonicalUtcText(
            session.StartedAtUtc,
            nameof(session.StartedAtUtc));
        ProviderIssuerAuthority.RequireSha256(
            session.PublicKeySha256,
            nameof(session.PublicKeySha256));
        ProviderIssuerAuthority.ExactText(
            session.SessionIdentity,
            nameof(session.SessionIdentity));

        byte[] publicKey = ProviderIssuerAuthority.DecodeCanonicalBase64(
            session.PublicKeySpkiBase64,
            nameof(session.PublicKeySpkiBase64));
        if (!string.Equals(
                ProviderIssuerAuthority.Sha256(publicKey),
                session.PublicKeySha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer public key digest is invalid");
        }

        string expectedIdentity = ProviderIssuerAuthority.ContentIdentity(
            "provider-issuer-session",
            ProviderIssuerAuthority.CanonicalMaterial(
                session.Schema,
                session.IssuerInstanceId,
                session.StartedAtUtc,
                session.PublicKeySpkiBase64,
                session.PublicKeySha256));
        if (!string.Equals(
                expectedIdentity,
                session.SessionIdentity,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer session identity is invalid");
        }
    }

    private static void RequireSignature(
        ProviderIssuerSession session,
        byte[] material,
        string signatureBase64,
        string subject)
    {
        byte[] signature = ProviderIssuerAuthority.DecodeCanonicalBase64(
            signatureBase64,
            subject + " signature");
        byte[] publicKey = ProviderIssuerAuthority.DecodeCanonicalBase64(
            session.PublicKeySpkiBase64,
            nameof(session.PublicKeySpkiBase64));
        using ECDsa verifier = ECDsa.Create();
        try
        {
            verifier.ImportSubjectPublicKeyInfo(
                publicKey,
                out int bytesRead);
            if (bytesRead != publicKey.Length)
            {
                throw new ProviderIssuerAuthorityException(
                    "provider issuer public key contains trailing data");
            }
        }
        catch (CryptographicException)
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer public key is not valid ECDSA SubjectPublicKeyInfo");
        }

        byte[] hash = SHA256.HashData(material);
        bool valid;
        try
        {
            valid = verifier.VerifyHash(
                hash,
                signature,
                DSASignatureFormat.IeeeP1363FixedFieldConcatenation);
        }
        catch (CryptographicException)
        {
            valid = false;
        }
        if (!valid)
        {
            throw new ProviderIssuerAuthorityException(
                subject + " signature is invalid");
        }
    }
}
