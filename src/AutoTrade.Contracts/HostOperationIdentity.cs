using System.Buffers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace AutoTrade.Contracts;

/// <summary>
/// Canonical deterministic identity for a host operation admitted from one scoped command.
/// This mirrors the versioned host contract and must not be replaced by response-provided authority.
/// </summary>
public static class HostOperationIdentity
{
    private static readonly Guid NamespaceUrl =
        Guid.Parse("6ba7b811-9dad-11d1-80b4-00c04fd430c8");

    public static string Derive(
        string accountId,
        string environment,
        string commandId)
    {
        if (string.IsNullOrWhiteSpace(accountId)
            || !string.Equals(accountId, accountId.Trim(), StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "accountId must be a canonical non-empty string.",
                nameof(accountId));
        }

        if (!CommonScalarContracts.IsValid("Environment", environment))
        {
            throw new ArgumentException(
                "environment must be a canonical Environment.",
                nameof(environment));
        }

        if (string.IsNullOrEmpty(commandId))
        {
            throw new ArgumentException(
                "commandId must be a non-empty string.",
                nameof(commandId));
        }

        ArrayBufferWriter<byte> scopeBuffer = new();
        using (Utf8JsonWriter writer = new(
            scopeBuffer,
            new JsonWriterOptions
            {
                Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
                Indented = false,
            }))
        {
            writer.WriteStartObject();
            writer.WriteString("account_id", accountId);
            writer.WriteString("environment", environment);
            writer.WriteEndObject();
        }

        string scopeDigest =
            Convert.ToHexString(SHA256.HashData(scopeBuffer.WrittenSpan))
                .ToLowerInvariant();
        string name =
            "https://operations.autotrade.local/host/"
            + scopeDigest
            + "/"
            + commandId;
        return Uuid5(NamespaceUrl, name).ToString("D");
    }

    private static Guid Uuid5(Guid namespaceId, string name)
    {
        Span<byte> namespaceBytes = stackalloc byte[16];
        if (!namespaceId.TryWriteBytes(
                namespaceBytes,
                bigEndian: true,
                out int bytesWritten)
            || bytesWritten != 16)
        {
            throw new InvalidOperationException(
                "Failed to encode UUID namespace.");
        }

        byte[] nameBytes = Encoding.UTF8.GetBytes(name);
        byte[] material = new byte[16 + nameBytes.Length];
        namespaceBytes.CopyTo(material);
        nameBytes.CopyTo(material.AsSpan(16));

        byte[] digest = SHA1.HashData(material);
        digest[6] = (byte)((digest[6] & 0x0f) | 0x50);
        digest[8] = (byte)((digest[8] & 0x3f) | 0x80);
        return new Guid(digest.AsSpan(0, 16), bigEndian: true);
    }
}
