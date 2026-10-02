using System;
using System.Buffers;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace AutoTrade.Host;

internal sealed class ProviderAuthenticatedReadEvidence
{
    private readonly byte[] _responseBytes;

    internal ProviderAuthenticatedReadEvidence(
        ProviderIssuerSession issuerSession,
        ProviderAuthenticatedReadAttemptBinding attempt,
        ProviderAuthenticatedReadReceipt receipt,
        ProviderAuthenticatedReadDurabilityReceipt durabilityReceipt,
        byte[] responseBytes)
    {
        ArgumentNullException.ThrowIfNull(issuerSession);
        ArgumentNullException.ThrowIfNull(attempt);
        ArgumentNullException.ThrowIfNull(receipt);
        ArgumentNullException.ThrowIfNull(durabilityReceipt);
        ArgumentNullException.ThrowIfNull(responseBytes);
        if (responseBytes.Length == 0)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated read evidence requires exact non-empty response bytes");
        }

        IssuerSession = issuerSession;
        Attempt = attempt;
        Receipt = receipt;
        DurabilityReceipt = durabilityReceipt;
        _responseBytes = responseBytes.ToArray();
    }

    internal ProviderIssuerSession IssuerSession { get; }
    internal ProviderAuthenticatedReadAttemptBinding Attempt { get; }
    internal ProviderAuthenticatedReadReceipt Receipt { get; }
    internal ProviderAuthenticatedReadDurabilityReceipt DurabilityReceipt { get; }
    internal ReadOnlyMemory<byte> ResponseBytes => _responseBytes;
    internal byte[] CopyResponseBytes() => _responseBytes.ToArray();
}

/// <summary>
/// Credential-bearing no-retry Bybit authenticated HTTP read cut.
///
/// The caller supplies only a requested read scope. Terminal entity/C/Q/build,
/// endpoint-rule, network-policy and transport identities come from the canonical
/// current-authority boundary. The client derives the absolute Bybit origin from
/// the exact provider_environment, keeps the resolved credential generation alive
/// through the wire call, issues a Host-signed Prepared binding immediately before
/// send, and signs only the exact definitive response bytes received on that cut.
///
/// There is intentionally no injected HttpClient/handler/wire callback surface:
/// test or parser bytes must not be able to mint PROVIDER_ORIGIN.
/// </summary>
internal sealed class BybitAuthenticatedReadClient
{
    private const int RecvWindowMilliseconds = 5000;
    private const int MaxResponseBytes = 16 * 1024 * 1024;
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(15);

    private readonly IProviderAuthenticatedReadAuthorityBoundary _currentAuthority;
    private readonly IProviderCredentialMaterialSource _credentialSource;
    private readonly ProviderIssuerAuthority _issuer;
    private readonly IProviderAuthenticatedReadDurabilityBoundary _durability;

    internal BybitAuthenticatedReadClient(
        IProviderAuthenticatedReadAuthorityBoundary currentAuthority,
        IProviderCredentialMaterialSource credentialSource,
        ProviderIssuerAuthority issuer)
        : this(
            currentAuthority,
            credentialSource,
            issuer,
            new UnavailableProviderAuthenticatedReadDurabilityBoundary())
    {
    }

    internal BybitAuthenticatedReadClient(
        IProviderAuthenticatedReadAuthorityBoundary currentAuthority,
        IProviderCredentialMaterialSource credentialSource,
        ProviderIssuerAuthority issuer,
        IProviderAuthenticatedReadDurabilityBoundary durability)
    {
        _currentAuthority =
            currentAuthority ?? throw new ArgumentNullException(nameof(currentAuthority));
        _credentialSource =
            credentialSource ?? throw new ArgumentNullException(nameof(credentialSource));
        _issuer = issuer ?? throw new ArgumentNullException(nameof(issuer));
        _durability =
            durability ?? throw new ArgumentNullException(nameof(durability));
    }

    internal async Task<ProviderAuthenticatedReadEvidence> ExecuteAsync(
        ProviderAuthenticatedReadRequest request,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);

        ProviderCurrentAuthenticatedReadAuthority authority =
            _currentAuthority.RequireCurrent(request);
        RequireRequestMatchesAuthority(request, authority);
        RequireBybitAuthority(authority);
        _currentAuthority.RequireStillCurrent(authority);

        ProviderAuthenticatedReadAttemptBinding? attempt = null;
        bool attemptOpen = false;
        ProviderAuthenticatedReadPreparedEvidence? preparedEvidence = null;

        try
        {
            // Resolve only long enough to bind the exact current generation into
            // the signed Host attempt. Secret bytes are zeroed before the durable
            // Prepared boundary is invoked and are never retained in the evidence.
            using (ResolvedProviderCredential preparedCredential =
                _credentialSource.Resolve(
                    authority,
                    authority.CredentialHandleId,
                    "READ"))
            {
                RequireResolvedCredentialMatchesAuthority(
                    preparedCredential,
                    authority,
                    expectedGeneration: null);

                ProviderAuthenticatedReadSubject subject = new(
                    ProviderId: authority.ProviderId,
                    AccountId: authority.AccountId,
                    EntityId: authority.EntityId,
                    RuntimeEnvironment: authority.RuntimeEnvironment,
                    ProviderEnvironment: authority.ProviderEnvironment,
                    Endpoint: authority.Endpoint,
                    Surface: authority.Surface,
                    PermissionScope: authority.PermissionScope,
                    DataEntitlement: authority.DataEntitlement,
                    InstrumentVersion: authority.InstrumentVersion,
                    QueryDigest: authority.QueryDigest,
                    EndpointRuleIdentity: authority.EndpointRuleIdentity,
                    CredentialHandleId: preparedCredential.HandleId,
                    CredentialGeneration: preparedCredential.Generation,
                    CapabilityId: authority.CapabilityId,
                    QualificationId: authority.QualificationId,
                    QualificationBuildId: authority.QualificationBuildId,
                    AdapterBuildIdentity: authority.AdapterBuildIdentity,
                    NetworkPolicyIdentity: authority.NetworkPolicyIdentity,
                    TransportIdentity: authority.TransportIdentity);
                ProviderIssuerAuthority.RequireReadSubject(subject);

                // The first currentness recheck is after credential-generation
                // resolution and immediately before the process issuer creates
                // the Prepared evidence. No wire request has been built or sent.
                _currentAuthority.RequireStillCurrent(authority);
                attempt = _issuer.IssueAuthenticatedReadAttempt(
                    subject,
                    DateTimeOffset.UtcNow);
                attemptOpen = true;
            }

            preparedEvidence = new ProviderAuthenticatedReadPreparedEvidence(
                _issuer.Session,
                attempt,
                authority.Query);

            // This is the mandatory cross-authority barrier. A real implementation
            // must durably commit the exact signed attempt before returning. The
            // only product implementation today is fail-closed, so no authenticated
            // provider read can reach the network until the journal bridge exists.
            ProviderAuthenticatedReadDurabilityReceipt durabilityReceipt =
                _durability.CommitPrepared(preparedEvidence);
            ProviderAuthenticatedReadDurabilityVerifier.RequireMatches(
                preparedEvidence,
                durabilityReceipt);

            // The durability wait is intentionally outside the final wire cut.
            // Re-resolve every mutable authority after it and require the exact
            // same terminal route/C+Q/query identity before touching credentials.
            ProviderCurrentAuthenticatedReadAuthority current =
                _currentAuthority.RequireCurrent(request);
            RequireRequestMatchesAuthority(request, current);
            RequireExactAuthoritySnapshot(authority, current);
            RequireBybitAuthority(current);
            _currentAuthority.RequireStillCurrent(current);

            using ResolvedProviderCredential resolved =
                _credentialSource.Resolve(
                    current,
                    current.CredentialHandleId,
                    "READ");
            RequireResolvedCredentialMatchesAuthority(
                resolved,
                current,
                expectedGeneration: attempt.Subject.CredentialGeneration);

            using BybitCredentialMaterial credential =
                BybitCredentialMaterial.Parse(resolved.SecretBytes);
            long timestampMilliseconds =
                DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            using HttpRequestMessage outbound =
                BuildRequestMessage(current, credential, timestampMilliseconds);

            // No credential resolution, signing, hostname selection, query
            // mutation or waits may occur after this revalidation and before send.
            _currentAuthority.RequireStillCurrent(current);

            using SocketsHttpHandler handler = NewWireHandler();
            using HttpMessageInvoker invoker = new(handler, disposeHandler: false);
            using CancellationTokenSource timeout =
                CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(RequestTimeout);

            using HttpResponseMessage response =
                await invoker.SendAsync(outbound, timeout.Token).ConfigureAwait(false);
            int httpStatus = (int)response.StatusCode;
            if (httpStatus < 100 || httpStatus > 599)
            {
                throw new ProviderIssuerAuthorityException(
                    "provider returned an invalid HTTP status");
            }

            byte[] responseBytes =
                await ReadBoundedResponseAsync(response, timeout.Token)
                    .ConfigureAwait(false);

            ProviderAuthenticatedReadReceipt receipt =
                _issuer.IssueAuthenticatedReadReceipt(
                    attempt,
                    httpStatus,
                    responseBytes,
                    DateTimeOffset.UtcNow);
            attemptOpen = false;

            ProviderIssuerVerifier.RequireValidReadReceipt(
                _issuer.Session,
                attempt,
                receipt,
                responseBytes,
                _issuer.Session.SessionIdentity,
                _issuer.Session.PublicKeySha256);

            return new ProviderAuthenticatedReadEvidence(
                _issuer.Session,
                attempt,
                receipt,
                durabilityReceipt,
                responseBytes);
        }
        catch
        {
            if (attemptOpen && attempt is not null)
            {
                _issuer.AbandonAuthenticatedReadAttempt(attempt);
            }
            throw;
        }
    }

    private static void RequireResolvedCredentialMatchesAuthority(
        ResolvedProviderCredential resolved,
        ProviderCurrentAuthenticatedReadAuthority authority,
        long? expectedGeneration)
    {
        ArgumentNullException.ThrowIfNull(resolved);
        ArgumentNullException.ThrowIfNull(authority);
        if (!string.Equals(
                resolved.HandleId,
                authority.CredentialHandleId,
                StringComparison.Ordinal)
            || !string.Equals(resolved.Purpose, "READ", StringComparison.Ordinal)
            || resolved.Generation <= 0
            || (expectedGeneration is not null
                && resolved.Generation != expectedGeneration.Value))
        {
            throw new ProviderIssuerAuthorityException(
                "resolved credential does not match current authenticated-read authority generation");
        }
    }

    internal static void RequireExactAuthoritySnapshot(
        ProviderCurrentAuthenticatedReadAuthority expected,
        ProviderCurrentAuthenticatedReadAuthority current)
    {
        ArgumentNullException.ThrowIfNull(expected);
        ArgumentNullException.ThrowIfNull(current);

        bool same =
            string.Equals(expected.ProviderId, current.ProviderId, StringComparison.Ordinal)
            && string.Equals(expected.AccountId, current.AccountId, StringComparison.Ordinal)
            && string.Equals(expected.EntityId, current.EntityId, StringComparison.Ordinal)
            && string.Equals(
                expected.RuntimeEnvironment,
                current.RuntimeEnvironment,
                StringComparison.Ordinal)
            && string.Equals(
                expected.ProviderEnvironment,
                current.ProviderEnvironment,
                StringComparison.Ordinal)
            && string.Equals(expected.Endpoint, current.Endpoint, StringComparison.Ordinal)
            && string.Equals(expected.Surface, current.Surface, StringComparison.Ordinal)
            && string.Equals(
                expected.PermissionScope,
                current.PermissionScope,
                StringComparison.Ordinal)
            && string.Equals(
                expected.DataEntitlement,
                current.DataEntitlement,
                StringComparison.Ordinal)
            && string.Equals(
                expected.InstrumentVersion,
                current.InstrumentVersion,
                StringComparison.Ordinal)
            && string.Equals(
                expected.QueryDigest,
                current.QueryDigest,
                StringComparison.Ordinal)
            && string.Equals(
                expected.EndpointRuleIdentity,
                current.EndpointRuleIdentity,
                StringComparison.Ordinal)
            && string.Equals(
                expected.CredentialHandleId,
                current.CredentialHandleId,
                StringComparison.Ordinal)
            && string.Equals(
                expected.CapabilityId,
                current.CapabilityId,
                StringComparison.Ordinal)
            && string.Equals(
                expected.QualificationId,
                current.QualificationId,
                StringComparison.Ordinal)
            && string.Equals(
                expected.QualificationBuildId,
                current.QualificationBuildId,
                StringComparison.Ordinal)
            && string.Equals(
                expected.AdapterBuildIdentity,
                current.AdapterBuildIdentity,
                StringComparison.Ordinal)
            && string.Equals(
                expected.NetworkPolicyIdentity,
                current.NetworkPolicyIdentity,
                StringComparison.Ordinal)
            && string.Equals(
                expected.TransportIdentity,
                current.TransportIdentity,
                StringComparison.Ordinal)
            && string.Equals(
                CanonicalQuery(expected.Query),
                CanonicalQuery(current.Query),
                StringComparison.Ordinal);
        if (!same)
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated-read current authority changed across durable Prepared barrier");
        }
    }

    internal static HttpRequestMessage BuildRequestMessage(
        ProviderCurrentAuthenticatedReadAuthority authority,
        BybitCredentialMaterial credential,
        long timestampMilliseconds)
    {
        ArgumentNullException.ThrowIfNull(authority);
        ArgumentNullException.ThrowIfNull(credential);
        RequireBybitAuthority(authority);
        if (timestampMilliseconds <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "Bybit authenticated-read timestamp must be positive");
        }

        string query = CanonicalQuery(authority.Query);
        Uri baseUri = ProviderBaseUri(
            authority.RuntimeEnvironment,
            authority.ProviderEnvironment);
        Uri requestUri = new(baseUri, authority.Endpoint + "?" + query);

        byte[] apiKey = credential.ApiKey.ToArray();
        byte[] apiSecret = credential.ApiSecret.ToArray();
        byte[] signingMaterial = Array.Empty<byte>();
        byte[] signatureBytes = Array.Empty<byte>();
        try
        {
            string apiKeyText = Encoding.ASCII.GetString(apiKey);
            string signingText =
                timestampMilliseconds.ToString(CultureInfo.InvariantCulture)
                + apiKeyText
                + RecvWindowMilliseconds.ToString(CultureInfo.InvariantCulture)
                + query;
            signingMaterial = Encoding.ASCII.GetBytes(signingText);
            using HMACSHA256 hmac = new(apiSecret);
            signatureBytes = hmac.ComputeHash(signingMaterial);
            string signature = Convert.ToHexString(signatureBytes).ToLowerInvariant();

            HttpRequestMessage request = new(HttpMethod.Get, requestUri);
            request.Headers.TryAddWithoutValidation("Accept", "application/json");
            request.Headers.TryAddWithoutValidation("X-BAPI-API-KEY", apiKeyText);
            request.Headers.TryAddWithoutValidation(
                "X-BAPI-TIMESTAMP",
                timestampMilliseconds.ToString(CultureInfo.InvariantCulture));
            request.Headers.TryAddWithoutValidation(
                "X-BAPI-RECV-WINDOW",
                RecvWindowMilliseconds.ToString(CultureInfo.InvariantCulture));
            request.Headers.TryAddWithoutValidation("X-BAPI-SIGN", signature);
            return request;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(apiKey);
            CryptographicOperations.ZeroMemory(apiSecret);
            if (signingMaterial.Length != 0)
            {
                CryptographicOperations.ZeroMemory(signingMaterial);
            }
            if (signatureBytes.Length != 0)
            {
                CryptographicOperations.ZeroMemory(signatureBytes);
            }
        }
    }

    internal static string CanonicalQuery(IReadOnlyDictionary<string, string> query)
    {
        ArgumentNullException.ThrowIfNull(query);
        if (query.Count == 0 || query.Count > 64)
        {
            throw new ProviderIssuerAuthorityException(
                "Bybit authenticated-read query cardinality is invalid");
        }

        List<KeyValuePair<string, string>> items = new(query.Count);
        foreach ((string rawKey, string rawValue) in query)
        {
            string key = RequireQueryText(rawKey, "query key", allowEmpty: false);
            string value = RequireQueryText(rawValue, "query value", allowEmpty: true);
            items.Add(new KeyValuePair<string, string>(key, value));
        }
        items.Sort(static (left, right) =>
            StringComparer.Ordinal.Compare(left.Key, right.Key));

        StringBuilder encoded = new();
        foreach (KeyValuePair<string, string> item in items)
        {
            if (encoded.Length != 0)
            {
                encoded.Append('&');
            }
            encoded.Append(EncodeQueryComponent(item.Key));
            encoded.Append('=');
            encoded.Append(EncodeQueryComponent(item.Value));
            if (encoded.Length > 8192)
            {
                throw new ProviderIssuerAuthorityException(
                    "Bybit authenticated-read canonical query exceeds bound");
            }
        }
        return encoded.ToString();
    }

    internal static Uri ProviderBaseUri(
        string runtimeEnvironment,
        string providerEnvironment)
    {
        ProviderIssuerAuthority.ExactText(
            runtimeEnvironment,
            nameof(runtimeEnvironment));
        ProviderIssuerAuthority.ExactText(
            providerEnvironment,
            nameof(providerEnvironment));

        return (runtimeEnvironment, providerEnvironment) switch
        {
            ("LIVE", "MAINNET") =>
                new Uri("https://api.bybit.com", UriKind.Absolute),
            ("PAPER", "TESTNET") =>
                new Uri("https://api-testnet.bybit.com", UriKind.Absolute),
            ("PAPER", "DEMO") =>
                new Uri("https://api-demo.bybit.com", UriKind.Absolute),
            _ => throw new ProviderIssuerAuthorityException(
                "Bybit runtime/provider environment mapping is invalid"),
        };
    }

    internal static void RequireRequestMatchesAuthority(
        ProviderAuthenticatedReadRequest request,
        ProviderCurrentAuthenticatedReadAuthority authority)
    {
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(authority);

        ProviderIssuerAuthority.ExactText(request.ProviderId, nameof(request.ProviderId));
        ProviderIssuerAuthority.ExactText(request.AccountId, nameof(request.AccountId));
        ProviderIssuerAuthority.ExactText(
            request.RuntimeEnvironment,
            nameof(request.RuntimeEnvironment));
        ProviderIssuerAuthority.ExactText(
            request.ProviderEnvironment,
            nameof(request.ProviderEnvironment));
        ProviderIssuerAuthority.ExactText(request.Endpoint, nameof(request.Endpoint));
        ProviderIssuerAuthority.ExactText(request.Surface, nameof(request.Surface));
        ProviderIssuerAuthority.ExactText(
            request.PermissionScope,
            nameof(request.PermissionScope));
        ProviderIssuerAuthority.ExactText(
            request.InstrumentVersion,
            nameof(request.InstrumentVersion));
        ProviderIssuerAuthority.RequireSha256(
            request.QueryDigest,
            nameof(request.QueryDigest));
        ProviderIssuerAuthority.ExactText(
            request.CredentialHandleId,
            nameof(request.CredentialHandleId));

        if (!string.Equals(request.ProviderId, authority.ProviderId, StringComparison.Ordinal)
            || !string.Equals(request.AccountId, authority.AccountId, StringComparison.Ordinal)
            || !string.Equals(
                request.RuntimeEnvironment,
                authority.RuntimeEnvironment,
                StringComparison.Ordinal)
            || !string.Equals(
                request.ProviderEnvironment,
                authority.ProviderEnvironment,
                StringComparison.Ordinal)
            || !string.Equals(request.Endpoint, authority.Endpoint, StringComparison.Ordinal)
            || !string.Equals(request.Surface, authority.Surface, StringComparison.Ordinal)
            || !string.Equals(
                request.PermissionScope,
                authority.PermissionScope,
                StringComparison.Ordinal)
            || !string.Equals(
                request.InstrumentVersion,
                authority.InstrumentVersion,
                StringComparison.Ordinal)
            || !string.Equals(
                request.QueryDigest,
                authority.QueryDigest,
                StringComparison.Ordinal)
            || !string.Equals(
                request.CredentialHandleId,
                authority.CredentialHandleId,
                StringComparison.Ordinal)
            || !string.Equals(
                CanonicalQuery(request.Query),
                CanonicalQuery(authority.Query),
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "current authenticated-read authority does not match requested scope");
        }
    }

    internal static void RequireBybitAuthority(
        ProviderCurrentAuthenticatedReadAuthority authority)
    {
        ArgumentNullException.ThrowIfNull(authority);
        foreach ((string value, string name) in new[]
        {
            (authority.ProviderId, nameof(authority.ProviderId)),
            (authority.AccountId, nameof(authority.AccountId)),
            (authority.EntityId, nameof(authority.EntityId)),
            (authority.RuntimeEnvironment, nameof(authority.RuntimeEnvironment)),
            (authority.ProviderEnvironment, nameof(authority.ProviderEnvironment)),
            (authority.Endpoint, nameof(authority.Endpoint)),
            (authority.Surface, nameof(authority.Surface)),
            (authority.PermissionScope, nameof(authority.PermissionScope)),
            (authority.DataEntitlement, nameof(authority.DataEntitlement)),
            (authority.InstrumentVersion, nameof(authority.InstrumentVersion)),
            (authority.CredentialHandleId, nameof(authority.CredentialHandleId)),
            (authority.CapabilityId, nameof(authority.CapabilityId)),
            (authority.QualificationId, nameof(authority.QualificationId)),
            (authority.QualificationBuildId, nameof(authority.QualificationBuildId)),
            (authority.AdapterBuildIdentity, nameof(authority.AdapterBuildIdentity)),
            (authority.TransportIdentity, nameof(authority.TransportIdentity)),
        })
        {
            ProviderIssuerAuthority.ExactText(value, name);
        }
        ProviderIssuerAuthority.RequireSha256(
            authority.QueryDigest,
            nameof(authority.QueryDigest));
        ProviderIssuerAuthority.RequireSha256(
            authority.EndpointRuleIdentity,
            nameof(authority.EndpointRuleIdentity));
        ProviderIssuerAuthority.RequireSha256(
            authority.NetworkPolicyIdentity,
            nameof(authority.NetworkPolicyIdentity));

        if (!string.Equals(authority.ProviderId, "BYBIT", StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "authenticated-read Host cut currently supports BYBIT only");
        }
        _ = ProviderBaseUri(
            authority.RuntimeEnvironment,
            authority.ProviderEnvironment);
        if (!authority.Endpoint.StartsWith("/v5/", StringComparison.Ordinal)
            || authority.Endpoint.Contains("://", StringComparison.Ordinal)
            || authority.Endpoint.Contains('?')
            || authority.Endpoint.Contains('#')
            || authority.Endpoint.Contains("..", StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Bybit authenticated-read endpoint must be a canonical /v5/ path");
        }
        if (!string.Equals(
                authority.Surface,
                "AUTHENTICATED_READ",
                StringComparison.Ordinal)
            && !string.Equals(
                authority.Surface,
                "ACTIVITIES",
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Bybit authenticated-read surface is invalid");
        }
        _ = CanonicalQuery(authority.Query);
    }

    private static SocketsHttpHandler NewWireHandler() => new()
    {
        UseProxy = false,
        UseCookies = false,
        AllowAutoRedirect = false,
        AutomaticDecompression = DecompressionMethods.None,
        ConnectTimeout = RequestTimeout,
        MaxResponseHeadersLength = 64,
    };

    private static async Task<byte[]> ReadBoundedResponseAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(response);
        long? declaredLength = response.Content.Headers.ContentLength;
        if (declaredLength is > MaxResponseBytes)
        {
            throw new ProviderIssuerAuthorityException(
                "provider response exceeds authenticated-read byte budget");
        }

        await using Stream source =
            await response.Content.ReadAsStreamAsync(cancellationToken)
                .ConfigureAwait(false);
        using MemoryStream sink = new();
        byte[] buffer = ArrayPool<byte>.Shared.Rent(64 * 1024);
        try
        {
            while (true)
            {
                int read = await source.ReadAsync(
                    buffer.AsMemory(0, buffer.Length),
                    cancellationToken).ConfigureAwait(false);
                if (read == 0)
                {
                    break;
                }
                if (sink.Length + read > MaxResponseBytes)
                {
                    throw new ProviderIssuerAuthorityException(
                        "provider response exceeds authenticated-read byte budget");
                }
                sink.Write(buffer, 0, read);
            }
            if (sink.Length == 0)
            {
                throw new ProviderIssuerAuthorityException(
                    "provider returned an empty authenticated-read response");
            }
            return sink.ToArray();
        }
        finally
        {
            CryptographicOperations.ZeroMemory(buffer);
            ArrayPool<byte>.Shared.Return(buffer, clearArray: true);
        }
    }

    private static string RequireQueryText(
        string value,
        string name,
        bool allowEmpty)
    {
        if (value is null
            || (!allowEmpty && value.Length == 0)
            || value.Length > 2048
            || !string.Equals(value, value.Trim(), StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "Bybit authenticated-read " + name + " is not canonical text");
        }
        foreach (char item in value)
        {
            if (item > 0x7f || item < 0x20)
            {
                throw new ProviderIssuerAuthorityException(
                    "Bybit authenticated-read " + name + " must be printable ASCII");
            }
        }
        return value;
    }

    private static string EncodeQueryComponent(string value)
    {
        StringBuilder output = new();
        foreach (byte item in Encoding.ASCII.GetBytes(value))
        {
            if ((item >= (byte)'a' && item <= (byte)'z')
                || (item >= (byte)'A' && item <= (byte)'Z')
                || (item >= (byte)'0' && item <= (byte)'9')
                || item == (byte)'-'
                || item == (byte)'_'
                || item == (byte)'.'
                || item == (byte)'~')
            {
                output.Append((char)item);
            }
            else if (item == (byte)' ')
            {
                output.Append('+');
            }
            else
            {
                output.Append('%');
                output.Append(item.ToString("X2", CultureInfo.InvariantCulture));
            }
        }
        return output.ToString();
    }
}
