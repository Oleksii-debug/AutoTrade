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

/// <summary>
/// Process-bound signer for one canonical authenticated provider read.
///
/// This authority deliberately does not open sockets, resolve credentials,
/// select capability/qualification state, interpret provider economics, or
/// grant PAPER/LIVE financial authority by itself. Product composition must
/// supply only already-resolved exact Host-owned read subjects, and downstream
/// consumers must independently pin this Host issuer session before accepting
/// either a Prepared binding or a response receipt.
/// </summary>
internal sealed class ProviderIssuerAuthority : IDisposable
{
    internal const string SessionSchema = "autotrade-provider-issuer-session:v1";
    internal const string ReadAttemptSchema =
        "autotrade-provider-authenticated-read-attempt:v1";
    internal const string ReadReceiptSchema =
        "autotrade-provider-authenticated-read-receipt:v1";

    private static readonly HashSet<string> FinancialRuntimes =
        new(StringComparer.Ordinal) { "PAPER", "LIVE" };
    private static readonly HashSet<string> ReadSurfaces =
        new(StringComparer.Ordinal) { "AUTHENTICATED_READ", "ACTIVITIES" };

    private readonly object _gate = new();
    private readonly ECDsa _signingKey;
    private readonly Dictionary<string, long> _readGenerations =
        new(StringComparer.Ordinal);
    private readonly Dictionary<string, string> _openReadAttempts =
        new(StringComparer.Ordinal);
    private bool _disposed;

    private ProviderIssuerAuthority(
        ECDsa signingKey,
        ProviderIssuerSession session)
    {
        _signingKey = signingKey;
        Session = session;
    }

    public ProviderIssuerSession Session { get; }

    public static ProviderIssuerAuthority CreateProcessAuthority(
        DateTimeOffset startedAtUtc)
    {
        string startedAt = CanonicalUtc(startedAtUtc, nameof(startedAtUtc));
        ECDsa key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        try
        {
            byte[] publicKey = key.ExportSubjectPublicKeyInfo();
            string publicKeyBase64 = Convert.ToBase64String(publicKey);
            string publicKeySha256 = Sha256(publicKey);
            string issuerInstanceId =
                "provider-issuer:" + Guid.NewGuid().ToString("N");
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

    public ProviderAuthenticatedReadAttemptBinding IssueAuthenticatedReadAttempt(
        ProviderAuthenticatedReadSubject subject,
        DateTimeOffset preparedAtUtc)
    {
        ArgumentNullException.ThrowIfNull(subject);
        RequireReadSubject(subject);
        string preparedAt = CanonicalUtc(
            preparedAtUtc,
            nameof(preparedAtUtc));

        lock (_gate)
        {
            ThrowIfDisposed();
            string scope = ReadScope(subject);
            long generation = _readGenerations.TryGetValue(
                scope,
                out long current)
                ? checked(current + 1)
                : 1;
            _readGenerations[scope] = generation;

            string readAttemptId =
                "provider-read:" + Guid.NewGuid().ToString("N");
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
                    out string? issuedBindingSha256)
                || !string.Equals(
                    issuedBindingSha256,
                    attempt.BindingSha256,
                    StringComparison.Ordinal))
            {
                throw new ProviderIssuerAuthorityException(
                    "authenticated read attempt was not issued by this live Host issuer instance");
            }

            DateTimeOffset prepared = ParseCanonicalUtc(
                attempt.PreparedAtUtc,
                nameof(attempt.PreparedAtUtc));
            DateTimeOffset observed = ParseCanonicalUtc(
                observedAt,
                nameof(observedAtUtc));
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
                    out string? issuedBindingSha256)
                || !string.Equals(
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

    public void Dispose()
    {
        lock (_gate)
        {
            if (_disposed)
            {
                return;
            }
            _disposed = true;
            _readGenerations.Clear();
            _openReadAttempts.Clear();
            _signingKey.Dispose();
        }
        GC.SuppressFinalize(this);
    }

    internal static void RequireReadSubject(
        ProviderAuthenticatedReadSubject subject)
    {
        ExactUpperText(subject.ProviderId, nameof(subject.ProviderId));
        ExactText(subject.AccountId, nameof(subject.AccountId));
        ExactText(subject.EntityId, nameof(subject.EntityId));
        ExactUpperText(
            subject.RuntimeEnvironment,
            nameof(subject.RuntimeEnvironment));
        if (!FinancialRuntimes.Contains(subject.RuntimeEnvironment))
        {
            throw new ProviderIssuerAuthorityException(
                "provider read issuer is restricted to PAPER/LIVE runtime environments");
        }
        ExactUpperText(
            subject.ProviderEnvironment,
            nameof(subject.ProviderEnvironment));
        ExactText(subject.Endpoint, nameof(subject.Endpoint));
        if (!subject.Endpoint.StartsWith("/", StringComparison.Ordinal)
            || subject.Endpoint.StartsWith("//", StringComparison.Ordinal)
            || subject.Endpoint.Contains("://", StringComparison.Ordinal)
            || subject.Endpoint.Contains('?', StringComparison.Ordinal)
            || subject.Endpoint.Contains('#', StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Endpoint must be one canonical provider-relative path");
        }
        ExactUpperText(subject.Surface, nameof(subject.Surface));
        if (!ReadSurfaces.Contains(subject.Surface))
        {
            throw new ProviderIssuerAuthorityException(
                "Surface must be AUTHENTICATED_READ or ACTIVITIES");
        }
        ExactText(subject.PermissionScope, nameof(subject.PermissionScope));
        ExactText(subject.DataEntitlement, nameof(subject.DataEntitlement));
        ExactText(subject.InstrumentVersion, nameof(subject.InstrumentVersion));
        RequireSha256(subject.QueryDigest, nameof(subject.QueryDigest));
        RequireSha256(
            subject.EndpointRuleIdentity,
            nameof(subject.EndpointRuleIdentity));
        ExactText(
            subject.CredentialHandleId,
            nameof(subject.CredentialHandleId));
        if (subject.CredentialGeneration <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "CredentialGeneration must be positive");
        }
        ExactText(subject.CapabilityId, nameof(subject.CapabilityId));
        ExactText(subject.QualificationId, nameof(subject.QualificationId));
        ExactText(
            subject.QualificationBuildId,
            nameof(subject.QualificationBuildId));
        ExactText(
            subject.AdapterBuildIdentity,
            nameof(subject.AdapterBuildIdentity));
        RequireSha256(
            subject.NetworkPolicyIdentity,
            nameof(subject.NetworkPolicyIdentity));
        ExactText(subject.TransportIdentity, nameof(subject.TransportIdentity));
    }

    private static string ReadScope(
        ProviderAuthenticatedReadSubject subject)
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
                    subject.CredentialGeneration.ToString(
                        CultureInfo.InvariantCulture),
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
            subject.CredentialGeneration.ToString(
                CultureInfo.InvariantCulture),
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

    internal static byte[] CanonicalMaterial(params string[] fields)
    {
        ArgumentNullException.ThrowIfNull(fields);
        using MemoryStream stream = new();
        Span<byte> prefix = stackalloc byte[4];
        BinaryPrimitives.WriteInt32BigEndian(prefix, fields.Length);
        stream.Write(prefix);
        foreach (string field in fields)
        {
            string exact = field
                ?? throw new ProviderIssuerAuthorityException(
                    "canonical field cannot be null");
            byte[] encoded = Encoding.UTF8.GetBytes(exact);
            BinaryPrimitives.WriteInt32BigEndian(prefix, encoded.Length);
            stream.Write(prefix);
            stream.Write(encoded);
        }
        return stream.ToArray();
    }

    internal static string Sha256(byte[] value)
    {
        ArgumentNullException.ThrowIfNull(value);
        return "sha256:"
            + Convert.ToHexString(SHA256.HashData(value)).ToLowerInvariant();
    }

    internal static string ContentIdentity(string prefix, byte[] value)
    {
        ExactText(prefix, nameof(prefix));
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
        DateTimeOffset parsed = ParseCanonicalUtc(value, name);
        if (!string.Equals(
                CanonicalUtc(parsed, name),
                value,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical UTC text");
        }
    }

    internal static DateTimeOffset ParseCanonicalUtc(string value, string name)
    {
        if (!DateTimeOffset.TryParseExact(
                value,
                "yyyy-MM-dd'T'HH:mm:ss.fffffff'Z'",
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal
                    | DateTimeStyles.AdjustToUniversal,
                out DateTimeOffset parsed))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical UTC text");
        }
        return parsed;
    }

    internal static void ExactText(string value, string name)
    {
        if (string.IsNullOrEmpty(value)
            || !string.Equals(
                value,
                value.Trim(),
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical non-empty text");
        }
    }

    private static void ExactUpperText(string value, string name)
    {
        ExactText(value, name);
        if (!string.Equals(
                value,
                value.ToUpperInvariant(),
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical uppercase text");
        }
    }

    internal static void RequireSha256(string value, string name)
    {
        ExactText(value, name);
        if (value.Length != 71
            || !value.StartsWith("sha256:", StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                name + " must be canonical sha256 text");
        }
        for (int index = 7; index < value.Length; index++)
        {
            char character = value[index];
            bool valid =
                (character >= '0' && character <= '9')
                || (character >= 'a' && character <= 'f');
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
                StringComparison.Ordinal)
            || !string.Equals(
                session.PublicKeySha256,
                expectedPublicKeySha256,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer session does not match the pinned Host authority");
        }
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
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.IssuerSessionIdentity,
                session.SessionIdentity,
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.ReadAttemptBindingSha256,
                attempt.BindingSha256,
                StringComparison.Ordinal)
            || !string.Equals(
                receipt.ReadAttemptId,
                attempt.ReadAttemptId,
                StringComparison.Ordinal)
            || receipt.ReadGeneration != attempt.ReadGeneration)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read receipt is not bound to the exact prepared attempt");
        }
        if (receipt.HttpStatus < 100
            || receipt.HttpStatus > 599
            || receipt.ResponseLength <= 0
            || receipt.ResponseLength != responseBytes.LongLength)
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

        DateTimeOffset prepared = ProviderIssuerAuthority.ParseCanonicalUtc(
            attempt.PreparedAtUtc,
            nameof(attempt.PreparedAtUtc));
        DateTimeOffset observed = ProviderIssuerAuthority.ParseCanonicalUtc(
            receipt.ObservedAtUtc,
            nameof(receipt.ObservedAtUtc));
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
                StringComparison.Ordinal)
            || !string.Equals(
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
        if (!attempt.ReadAttemptId.StartsWith(
                "provider-read:",
                StringComparison.Ordinal)
            || attempt.ReadAttemptId.Length
                != "provider-read:".Length + 32
            || attempt.ReadGeneration <= 0)
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
        if (!session.IssuerInstanceId.StartsWith(
                "provider-issuer:",
                StringComparison.Ordinal)
            || session.IssuerInstanceId.Length
                != "provider-issuer:".Length + 32)
        {
            throw new ProviderIssuerAuthorityException(
                "provider issuer instance identity is invalid");
        }
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
        if (signature.Length != 64)
        {
            throw new ProviderIssuerAuthorityException(
                subject + " signature length is invalid");
        }
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
