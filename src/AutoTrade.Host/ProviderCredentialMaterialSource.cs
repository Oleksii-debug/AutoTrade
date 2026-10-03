using System.Security.Cryptography;

namespace AutoTrade.Host;

internal sealed class ResolvedProviderCredential : IDisposable
{
    private byte[] _secretBytes;
    private bool _disposed;

    internal ResolvedProviderCredential(
        string handleId,
        long generation,
        string purpose,
        byte[] secretBytes)
    {
        ProviderIssuerAuthority.ExactText(handleId, nameof(handleId));
        ProviderIssuerAuthority.ExactText(purpose, nameof(purpose));
        if (generation <= 0)
        {
            throw new ProviderIssuerAuthorityException(
                "credential generation must be positive");
        }
        ArgumentNullException.ThrowIfNull(secretBytes);
        if (secretBytes.Length == 0)
        {
            throw new ProviderIssuerAuthorityException(
                "resolved credential material must not be empty");
        }
        HandleId = handleId;
        Generation = generation;
        Purpose = purpose;
        _secretBytes = secretBytes;
    }

    public string HandleId { get; }
    public long Generation { get; }
    public string Purpose { get; }

    internal ReadOnlySpan<byte> SecretBytes
    {
        get
        {
            if (_disposed)
            {
                throw new ObjectDisposedException(nameof(ResolvedProviderCredential));
            }
            return _secretBytes;
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        CryptographicOperations.ZeroMemory(_secretBytes);
        _secretBytes = Array.Empty<byte>();
        GC.SuppressFinalize(this);
    }
}

/// <summary>
/// Internal credential-material handoff. Implementations must prove the exact
/// canonical handle generation and scope before returning bytes. No production
/// implementation is supplied until the existing OS-backed credential authority
/// can bridge into AutoTrade.Host without creating a second secret store.
/// </summary>
internal interface IProviderCredentialMaterialSource
{
    ResolvedProviderCredential Resolve(
        ProviderCurrentRouteAuthority authority,
        string credentialHandleId,
        string purpose);

    ResolvedProviderCredential Resolve(
        ProviderCurrentAuthenticatedReadAuthority authority,
        string credentialHandleId,
        string purpose);
}

internal sealed class UnavailableProviderCredentialMaterialSource
    : IProviderCredentialMaterialSource
{
    public ResolvedProviderCredential Resolve(
        ProviderCurrentRouteAuthority authority,
        string credentialHandleId,
        string purpose)
    {
        ArgumentNullException.ThrowIfNull(authority);
        RequireRequest(credentialHandleId, purpose);
        throw Unavailable();
    }

    public ResolvedProviderCredential Resolve(
        ProviderCurrentAuthenticatedReadAuthority authority,
        string credentialHandleId,
        string purpose)
    {
        ArgumentNullException.ThrowIfNull(authority);
        RequireRequest(credentialHandleId, purpose);
        throw Unavailable();
    }

    private static void RequireRequest(
        string credentialHandleId,
        string purpose)
    {
        ProviderIssuerAuthority.ExactText(
            credentialHandleId,
            nameof(credentialHandleId));
        ProviderIssuerAuthority.ExactText(purpose, nameof(purpose));
    }

    private static ProviderIssuerAuthorityException Unavailable() =>
        new("canonical provider credential bridge is unavailable in AutoTrade.Host");
}
