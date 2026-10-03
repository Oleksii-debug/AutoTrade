using System.Buffers.Binary;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AutoTrade.Host;

internal sealed record BybitAuthenticationAcknowledgement(string ConnectionId);
internal sealed record BybitSubscriptionAcknowledgement(string ConnectionId);

/// <summary>
/// Provider-specific byte protocol for the qualified Bybit V5 private execution
/// stream. These helpers derive request bytes and validate exact control responses;
/// they do not grant provider origin. Only the sealed ClientWebSocket composition
/// may hand exact returned bytes to ProviderIssuerAuthority.
/// </summary>
internal static class BybitPrivateWebSocketProtocol
{
    public const string ProviderId = "BYBIT";
    public const string TopicId = "execution";
    public const string SubscriptionIdentity = "private-execution-v1";
    public const string TransportIdentity = "bybit-v5-private-websocket:v1";
    public const string CredentialPurpose = "READ";
    private const int AuthLifetimeMilliseconds = 1_000;

    public static void RequireRoute(ProviderCurrentRouteAuthority authority)
    {
        ArgumentNullException.ThrowIfNull(authority);
        if (!string.Equals(authority.ProviderId, ProviderId, StringComparison.Ordinal)
            || !string.Equals(authority.TopicId, TopicId, StringComparison.Ordinal)
            || !string.Equals(
                authority.SubscriptionIdentity,
                SubscriptionIdentity,
                StringComparison.Ordinal)
            || !string.Equals(
                authority.TransportIdentity,
                TransportIdentity,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "current provider route is not the qualified BYBIT execution stream");
        }

        string expectedEndpoint = authority.ProviderEnvironment switch
        {
            "MAINNET" when authority.RuntimeEnvironment == "LIVE" =>
                "wss://stream.bybit.com/v5/private",
            "TESTNET" when authority.RuntimeEnvironment == "PAPER" =>
                "wss://stream-testnet.bybit.com/v5/private",
            "DEMO" when authority.RuntimeEnvironment == "PAPER" =>
                "wss://stream-demo.bybit.com/v5/private",
            _ => throw new ProviderIssuerAuthorityException(
                "BYBIT runtime/provider environment pairing is not qualified"),
        };
        if (!string.Equals(authority.Endpoint, expectedEndpoint, StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT private endpoint does not match exact provider environment");
        }
        ProviderIssuerAuthority.RequireSha256(
            authority.NetworkPolicyIdentity,
            nameof(authority.NetworkPolicyIdentity));
        ProviderIssuerAuthority.ExactText(
            authority.CapabilityId,
            nameof(authority.CapabilityId));
        ProviderIssuerAuthority.ExactText(
            authority.QualificationId,
            nameof(authority.QualificationId));
        ProviderIssuerAuthority.ExactText(
            authority.QualificationBuildId,
            nameof(authority.QualificationBuildId));
    }

    public static byte[] BuildAuthenticationRequest(
        BybitCredentialMaterial credential,
        DateTimeOffset nowUtc)
    {
        ArgumentNullException.ThrowIfNull(credential);
        if (nowUtc == default)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT authentication requires a concrete Host clock instant");
        }
        long nowMillis = nowUtc.ToUniversalTime().ToUnixTimeMilliseconds();
        long expires = checked(nowMillis + AuthLifetimeMilliseconds);
        byte[] signMaterial = Encoding.ASCII.GetBytes(
            "GET/realtime" + expires.ToString(CultureInfo.InvariantCulture));
        byte[] signature;
        byte[] signingKey = credential.ApiSecret.ToArray();
        try
        {
            using HMACSHA256 hmac = new(signingKey);
            signature = hmac.ComputeHash(signMaterial);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(signingKey);
            CryptographicOperations.ZeroMemory(signMaterial);
        }

        string signatureHex = Convert.ToHexString(signature).ToLowerInvariant();
        CryptographicOperations.ZeroMemory(signature);
        byte[] prefix = Encoding.ASCII.GetBytes("{\"op\":\"auth\",\"args\":[\"");
        byte[] middle = Encoding.ASCII.GetBytes("\",");
        byte[] signaturePrefix = Encoding.ASCII.GetBytes(",\"");
        byte[] suffix = Encoding.ASCII.GetBytes("\"]}");
        byte[] expiryBytes = Encoding.ASCII.GetBytes(
            expires.ToString(CultureInfo.InvariantCulture));
        byte[] signatureBytes = Encoding.ASCII.GetBytes(signatureHex);
        byte[] output = new byte[
            prefix.Length
            + credential.ApiKey.Length
            + middle.Length
            + expiryBytes.Length
            + signaturePrefix.Length
            + signatureBytes.Length
            + suffix.Length];
        int offset = 0;
        prefix.CopyTo(output, offset);
        offset += prefix.Length;
        credential.ApiKey.CopyTo(output.AsSpan(offset));
        offset += credential.ApiKey.Length;
        middle.CopyTo(output, offset);
        offset += middle.Length;
        expiryBytes.CopyTo(output, offset);
        offset += expiryBytes.Length;
        signaturePrefix.CopyTo(output, offset);
        offset += signaturePrefix.Length;
        signatureBytes.CopyTo(output, offset);
        offset += signatureBytes.Length;
        suffix.CopyTo(output, offset);
        CryptographicOperations.ZeroMemory(signatureBytes);
        return output;
    }

    public static byte[] BuildSubscriptionRequest() =>
        Encoding.ASCII.GetBytes("{\"op\":\"subscribe\",\"args\":[\"execution\"]}");

    public static byte[] BuildHeartbeatRequest() =>
        Encoding.ASCII.GetBytes("{\"op\":\"ping\"}");

    public static BybitAuthenticationAcknowledgement ParseAuthenticationAcknowledgement(
        ReadOnlySpan<byte> payload)
    {
        using JsonDocument document = ParseStrictObject(payload, maxBytes: 32 * 1024);
        JsonElement root = document.RootElement;
        RequireExactProperties(root, "success", "ret_msg", "op", "conn_id");
        RequireSuccessfulControlResponse(root, "auth");
        return new BybitAuthenticationAcknowledgement(
            RequireControlText(root.GetProperty("conn_id"), "conn_id"));
    }

    public static BybitSubscriptionAcknowledgement ParseSubscriptionAcknowledgement(
        ReadOnlySpan<byte> payload,
        string expectedConnectionId)
    {
        ProviderIssuerAuthority.ExactText(
            expectedConnectionId,
            nameof(expectedConnectionId));
        using JsonDocument document = ParseStrictObject(payload, maxBytes: 32 * 1024);
        JsonElement root = document.RootElement;
        RequireExactProperties(root, "success", "ret_msg", "op", "conn_id");
        RequireSuccessfulControlResponse(root, "subscribe");
        string connectionId = RequireControlText(root.GetProperty("conn_id"), "conn_id");
        if (!string.Equals(connectionId, expectedConnectionId, StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT subscription acknowledgement changed connection identity");
        }
        return new BybitSubscriptionAcknowledgement(connectionId);
    }

    public static bool IsHeartbeatAcknowledgement(ReadOnlySpan<byte> payload)
    {
        try
        {
            using JsonDocument document = ParseStrictObject(payload, maxBytes: 32 * 1024);
            JsonElement root = document.RootElement;
            if (!TryUniqueProperty(root, "op", out JsonElement op)
                || op.ValueKind != JsonValueKind.String)
            {
                return false;
            }
            string value = op.GetString() ?? string.Empty;
            return value is "pong" or "ping";
        }
        catch (ProviderIssuerAuthorityException)
        {
            return false;
        }
    }

    public static void RequirePrivateExecutionFrame(ReadOnlySpan<byte> payload)
    {
        using JsonDocument document = ParseStrictObject(
            payload,
            maxBytes: 1024 * 1024);
        JsonElement root = document.RootElement;

        if (!TryUniqueProperty(root, "topic", out JsonElement topic)
            || topic.ValueKind != JsonValueKind.String
            || !string.Equals(
                topic.GetString(),
                TopicId,
                StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT private frame is not the qualified execution topic");
        }

        if (!TryUniqueProperty(root, "data", out JsonElement data)
            || data.ValueKind != JsonValueKind.Array
            || data.GetArrayLength() == 0)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT execution frame requires non-empty data array");
        }

        foreach (JsonElement item in data.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                throw new ProviderIssuerAuthorityException(
                    "BYBIT execution frame data entries must be objects");
            }
        }
    }

    public static byte[] CanonicalTranscript(
        ReadOnlySpan<byte> request,
        ReadOnlySpan<byte> response)
    {
        if (request.Length == 0 || response.Length == 0)
        {
            throw new ProviderIssuerAuthorityException(
                "provider control transcript requires request and response bytes");
        }
        byte[] transcript = new byte[checked(8 + request.Length + response.Length)];
        BinaryPrimitives.WriteInt32BigEndian(
            transcript.AsSpan(0, 4),
            request.Length);
        request.CopyTo(transcript.AsSpan(4, request.Length));
        int secondPrefix = checked(4 + request.Length);
        BinaryPrimitives.WriteInt32BigEndian(
            transcript.AsSpan(secondPrefix, 4),
            response.Length);
        response.CopyTo(transcript.AsSpan(secondPrefix + 4));
        return transcript;
    }

    private static JsonDocument ParseStrictObject(ReadOnlySpan<byte> payload, int maxBytes)
    {
        if (payload.Length == 0 || payload.Length > maxBytes)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT control response size is invalid");
        }
        try
        {
            JsonDocument document = JsonDocument.Parse(
                payload.ToArray(),
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 8,
                });
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                document.Dispose();
                throw new ProviderIssuerAuthorityException(
                    "BYBIT control response must be an object");
            }
            return document;
        }
        catch (JsonException error)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT control response is invalid JSON: " + error.GetType().Name);
        }
    }

    private static void RequireSuccessfulControlResponse(JsonElement root, string expectedOperation)
    {
        JsonElement success = root.GetProperty("success");
        if (success.ValueKind != JsonValueKind.True)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT " + expectedOperation + " was not acknowledged");
        }
        string operation = RequireControlText(root.GetProperty("op"), "op");
        if (!string.Equals(operation, expectedOperation, StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT control acknowledgement operation mismatch");
        }
        JsonElement ret = root.GetProperty("ret_msg");
        if (ret.ValueKind != JsonValueKind.String)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT control acknowledgement ret_msg is invalid");
        }
        string retMessage = ret.GetString() ?? string.Empty;
        if (!string.IsNullOrEmpty(retMessage))
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT control acknowledgement returned a non-empty result message");
        }
    }

    private static string RequireControlText(JsonElement element, string name)
    {
        if (element.ValueKind != JsonValueKind.String)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT " + name + " must be text");
        }
        string value = element.GetString() ?? string.Empty;
        if (string.IsNullOrEmpty(value)
            || value.Length > 256
            || !string.Equals(value, value.Trim(), StringComparison.Ordinal))
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT " + name + " is not canonical text");
        }
        return value;
    }

    private static void RequireExactProperties(JsonElement element, params string[] expectedNames)
    {
        HashSet<string> expected = new(expectedNames, StringComparer.Ordinal);
        HashSet<string> actual = new(StringComparer.Ordinal);
        foreach (JsonProperty property in element.EnumerateObject())
        {
            if (!actual.Add(property.Name))
            {
                throw new ProviderIssuerAuthorityException(
                    "BYBIT control response contains duplicate JSON key");
            }
        }
        if (!actual.SetEquals(expected))
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT control response fields are invalid");
        }
    }

    private static bool TryUniqueProperty(
        JsonElement element,
        string name,
        out JsonElement selected)
    {
        selected = default;
        bool found = false;
        HashSet<string> seen = new(StringComparer.Ordinal);
        foreach (JsonProperty property in element.EnumerateObject())
        {
            if (!seen.Add(property.Name))
            {
                throw new ProviderIssuerAuthorityException(
                    "BYBIT control response contains duplicate JSON key");
            }
            if (string.Equals(property.Name, name, StringComparison.Ordinal))
            {
                selected = property.Value;
                found = true;
            }
        }
        return found;
    }
}
