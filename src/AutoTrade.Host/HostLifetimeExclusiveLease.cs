using System;
using System.Threading;
using System.IO;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AutoTrade.Host;

/// <summary>
/// OS-enforced same-host exclusion for one exact financial provider-domain Host.
///
/// Windows composes a Global kernel-object existence fence with the retained
/// exclusive owner-record file. The kernel object prevents alternate/rebound
/// filesystem roots from creating parallel Host authority for the same scope.
///
/// This is a concurrency prerequisite only. It does not prove remote/copy-source
/// process death, credential revocation, or recovery takeover authority. Those
/// remain separate external #696 evidence requirements.
/// </summary>
public sealed class HostLifetimeExclusiveLease : IDisposable
{
    private const string Domain = "autotrade-host-lifetime-fence-v1";
    private const string KernelFencePrefix = "Global\\AutoTrade.Host.Lifetime.";
    private FileStream? _stream;
    private EventWaitHandle? _kernelFence;

    private HostLifetimeExclusiveLease(
        FileStream stream,
        EventWaitHandle? kernelFence,
        HostProcessOptions options,
        string scopeId,
        string ownerRecordSha256,
        string acquiredAtUtc)
    {
        _stream = stream;
        _kernelFence = kernelFence;
        Options = options;
        ScopeId = scopeId;
        OwnerRecordSha256 = ownerRecordSha256;
        AcquiredAtUtc = acquiredAtUtc;
    }

    internal HostProcessOptions Options { get; }
    internal string ScopeId { get; }
    internal string OwnerRecordSha256 { get; }
    internal string AcquiredAtUtc { get; }

    public static HostLifetimeExclusiveLease Acquire(HostProcessOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        string root = Environment.GetFolderPath(
            Environment.SpecialFolder.LocalApplicationData,
            Environment.SpecialFolderOption.Create);
        if (string.IsNullOrWhiteSpace(root))
        {
            throw new HostLifetimeFenceUnavailableException(
                "Host lifetime fence storage root is unavailable.");
        }
        return Acquire(options, Path.Combine(root, "AutoTrade", "host-fences"));
    }

    internal static HostLifetimeExclusiveLease Acquire(
        HostProcessOptions options,
        string rootDirectory)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (string.IsNullOrWhiteSpace(rootDirectory)
            || !string.Equals(rootDirectory, rootDirectory.Trim(), StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Host lifetime fence root must be canonical non-empty text.",
                nameof(rootDirectory));
        }

        string scopeId = ScopeIdFor(options);
        EventWaitHandle? kernelFence = AcquireKernelFence(scopeId);
        FileStream? stream = null;
        try
        {
            Directory.CreateDirectory(rootDirectory);
            string path = Path.Combine(rootDirectory, scopeId + ".lock");
            try
            {
                stream = new FileStream(
                    path,
                    FileMode.OpenOrCreate,
                    FileAccess.ReadWrite,
                    FileShare.None,
                    bufferSize: 4096,
                    options: FileOptions.WriteThrough);
            }
            catch (IOException error)
            {
                throw new HostLifetimeFenceUnavailableException(
                    "Another AutoTrade Host holds the same provider-domain lifetime fence.",
                    error);
            }
            catch (UnauthorizedAccessException error)
            {
                throw new HostLifetimeFenceUnavailableException(
                    "Host lifetime fence storage is not writable.",
                    error);
            }

            string acquiredAtUtc =
                HostContractTime.FormatUtcInstant(DateTimeOffset.UtcNow);
            string ownerRecordSha256 = WriteOwnerRecord(
                stream,
                options,
                scopeId,
                acquiredAtUtc);
            return new HostLifetimeExclusiveLease(
                stream,
                kernelFence,
                options,
                scopeId,
                ownerRecordSha256,
                acquiredAtUtc);
        }
        catch
        {
            stream?.Dispose();
            kernelFence?.Dispose();
            throw;
        }
    }

    internal static string ScopeIdFor(HostProcessOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        return ScopeIdFor(
            options.Provider,
            options.ProviderEnvironment,
            options.Environment,
            options.AccountId);
    }

    internal static string ScopeIdFor(
        string provider,
        string providerEnvironment,
        string environment,
        string accountId)
    {
        if (string.IsNullOrWhiteSpace(provider)
            || provider != provider.Trim()
            || string.IsNullOrWhiteSpace(providerEnvironment)
            || providerEnvironment != providerEnvironment.Trim()
            || string.IsNullOrWhiteSpace(environment)
            || environment != environment.Trim()
            || string.IsNullOrWhiteSpace(accountId)
            || accountId != accountId.Trim())
        {
            throw new HostLifetimeFenceUnavailableException(
                "Host lifetime fence financial scope is not canonical.");
        }
        byte[] material = Encoding.UTF8.GetBytes(
            Domain + "\0"
            + provider + "\0"
            + providerEnvironment + "\0"
            + environment + "\0"
            + accountId);
        try
        {
            return "hf-" + Convert.ToHexString(SHA256.HashData(material)).ToLowerInvariant();
        }
        finally
        {
            CryptographicOperations.ZeroMemory(material);
        }
    }

    private static EventWaitHandle? AcquireKernelFence(string scopeId)
    {
        if (!OperatingSystem.IsWindows())
        {
            return null;
        }
        try
        {
            EventWaitHandle fence = new(
                initialState: false,
                mode: EventResetMode.ManualReset,
                name: KernelFencePrefix + scopeId,
                createdNew: out bool createdNew);
            if (!createdNew)
            {
                fence.Dispose();
                throw new HostLifetimeFenceUnavailableException(
                    "Another AutoTrade Host holds the same provider-domain kernel fence.");
            }
            return fence;
        }
        catch (UnauthorizedAccessException error)
        {
            throw new HostLifetimeFenceUnavailableException(
                "Host lifetime kernel fence is unavailable.",
                error);
        }
    }

    private static string WriteOwnerRecord(
        FileStream stream,
        HostProcessOptions options,
        string scopeId,
        string acquiredAtUtc)
    {
        using Process process = Process.GetCurrentProcess();
        DateTimeOffset startedAt = process.StartTime.ToUniversalTime();
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(new
        {
            schema_version = 2,
            scope_id = scopeId,
            host_id = options.HostId,
            provider = options.Provider,
            provider_environment = options.ProviderEnvironment,
            runtime_environment = options.Environment,
            account_id = options.AccountId,
            process_id = Environment.ProcessId,
            process_started_at = HostContractTime.FormatUtcInstant(startedAt),
            acquired_at_utc = acquiredAtUtc,
        });
        try
        {
            string digest =
                "sha256:" + Convert.ToHexString(SHA256.HashData(payload)).ToLowerInvariant();
            stream.Position = 0;
            stream.SetLength(0);
            stream.Write(payload, 0, payload.Length);
            stream.Flush(flushToDisk: true);
            return digest;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(payload);
        }
    }

    internal void RequireHeld()
    {
        FileStream? stream = Volatile.Read(ref _stream);
        EventWaitHandle? kernelFence = Volatile.Read(ref _kernelFence);
        if (stream is null
            || stream.SafeFileHandle.IsClosed
            || stream.SafeFileHandle.IsInvalid
            || (OperatingSystem.IsWindows()
                && (kernelFence is null
                    || kernelFence.SafeWaitHandle.IsClosed
                    || kernelFence.SafeWaitHandle.IsInvalid)))
        {
            throw new HostLifetimeFenceUnavailableException(
                "Host lifetime fence is no longer held by this process.");
        }
    }

    public void Dispose()
    {
        FileStream? stream = Interlocked.Exchange(ref _stream, null);
        EventWaitHandle? kernelFence =
            Interlocked.Exchange(ref _kernelFence, null);
        try
        {
            stream?.Dispose();
        }
        finally
        {
            kernelFence?.Dispose();
        }
    }
}

public sealed class HostLifetimeFenceUnavailableException : InvalidOperationException
{
    public HostLifetimeFenceUnavailableException(string message)
        : base(message)
    {
    }

    public HostLifetimeFenceUnavailableException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}
