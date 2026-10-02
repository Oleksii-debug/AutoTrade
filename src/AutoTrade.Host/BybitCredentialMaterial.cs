using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AutoTrade.Host;

/// <summary>
/// Exact provider-secret payload consumed only inside the credential-bearing Host.
/// The canonical vault plaintext is UTF-8 JSON:
/// {"api_key":"...","api_secret":"..."}
/// with ASCII unescaped values. The secret is never returned as a managed string.
/// </summary>
internal sealed class BybitCredentialMaterial : IDisposable
{
    private byte[] _apiKey;
    private byte[] _apiSecret;
    private bool _disposed;

    private BybitCredentialMaterial(byte[] apiKey, byte[] apiSecret)
    {
        _apiKey = apiKey;
        _apiSecret = apiSecret;
    }

    internal ReadOnlySpan<byte> ApiKey
    {
        get
        {
            ThrowIfDisposed();
            return _apiKey;
        }
    }

    internal ReadOnlySpan<byte> ApiSecret
    {
        get
        {
            ThrowIfDisposed();
            return _apiSecret;
        }
    }

    public static BybitCredentialMaterial Parse(ReadOnlySpan<byte> payload)
    {
        if (payload.Length == 0 || payload.Length > 4096)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT credential payload size is invalid");
        }

        byte[] copy = payload.ToArray();
        try
        {
            using JsonDocument document = JsonDocument.Parse(
                copy,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 4,
                });
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new ProviderIssuerAuthorityException(
                    "BYBIT credential payload must be an object");
            }

            HashSet<string> names = new(StringComparer.Ordinal);
            foreach (JsonProperty property in root.EnumerateObject())
            {
                if (!names.Add(property.Name))
                {
                    throw new ProviderIssuerAuthorityException(
                        "BYBIT credential payload contains duplicate field");
                }
            }
            if (!names.SetEquals(new[] { "api_key", "api_secret" }))
            {
                throw new ProviderIssuerAuthorityException(
                    "BYBIT credential payload fields are invalid");
            }

            string apiKeyText = ExactAsciiSecretText(
                root.GetProperty("api_key"),
                "api_key",
                maxLength: 256);
            string apiSecretText = ExactAsciiSecretText(
                root.GetProperty("api_secret"),
                "api_secret",
                maxLength: 512);

            byte[] apiKey = Encoding.ASCII.GetBytes(apiKeyText);
            byte[] apiSecret = Encoding.ASCII.GetBytes(apiSecretText);
            byte[] canonical = CanonicalPayload(apiKey, apiSecret);
            try
            {
                if (!CryptographicOperations.FixedTimeEquals(copy, canonical))
                {
                    CryptographicOperations.ZeroMemory(apiKey);
                    CryptographicOperations.ZeroMemory(apiSecret);
                    throw new ProviderIssuerAuthorityException(
                        "BYBIT credential payload is not canonical JSON");
                }
            }
            finally
            {
                CryptographicOperations.ZeroMemory(canonical);
            }

            return new BybitCredentialMaterial(apiKey, apiSecret);
        }
        catch (JsonException error)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT credential payload is invalid JSON: " + error.GetType().Name);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(copy);
        }
    }

    internal static byte[] CanonicalPayload(
        ReadOnlySpan<byte> apiKey,
        ReadOnlySpan<byte> apiSecret)
    {
        RequireAsciiSecretBytes(apiKey, "api_key", 256);
        RequireAsciiSecretBytes(apiSecret, "api_secret", 512);
        byte[] prefix = Encoding.ASCII.GetBytes("{\"api_key\":\"");
        byte[] middle = Encoding.ASCII.GetBytes("\",\"api_secret\":\"");
        byte[] suffix = Encoding.ASCII.GetBytes("\"}");
        byte[] output = new byte[
            prefix.Length + apiKey.Length + middle.Length + apiSecret.Length + suffix.Length];
        int offset = 0;
        prefix.CopyTo(output, offset);
        offset += prefix.Length;
        apiKey.CopyTo(output.AsSpan(offset));
        offset += apiKey.Length;
        middle.CopyTo(output, offset);
        offset += middle.Length;
        apiSecret.CopyTo(output.AsSpan(offset));
        offset += apiSecret.Length;
        suffix.CopyTo(output, offset);
        return output;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        CryptographicOperations.ZeroMemory(_apiKey);
        CryptographicOperations.ZeroMemory(_apiSecret);
        _apiKey = Array.Empty<byte>();
        _apiSecret = Array.Empty<byte>();
        GC.SuppressFinalize(this);
    }

    private static string ExactAsciiSecretText(
        JsonElement element,
        string name,
        int maxLength)
    {
        if (element.ValueKind != JsonValueKind.String)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT " + name + " must be text");
        }
        string value = element.GetString() ?? string.Empty;
        if (value.Length == 0 || value.Length > maxLength)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT " + name + " length is invalid");
        }
        foreach (char character in value)
        {
            if (character < 0x21 || character > 0x7e
                || character == '"' || character == '\\')
            {
                throw new ProviderIssuerAuthorityException(
                    "BYBIT " + name + " contains unsupported characters");
            }
        }
        return value;
    }

    private static void RequireAsciiSecretBytes(
        ReadOnlySpan<byte> value,
        string name,
        int maxLength)
    {
        if (value.Length == 0 || value.Length > maxLength)
        {
            throw new ProviderIssuerAuthorityException(
                "BYBIT " + name + " length is invalid");
        }
        foreach (byte item in value)
        {
            if (item < 0x21 || item > 0x7e || item == (byte)'"' || item == (byte)'\\')
            {
                throw new ProviderIssuerAuthorityException(
                    "BYBIT " + name + " contains unsupported characters");
            }
        }
    }

    private void ThrowIfDisposed()
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(BybitCredentialMaterial));
        }
    }
}
