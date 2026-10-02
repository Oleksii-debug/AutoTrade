using System;
using System.Threading;
using System.IO;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AutoTrade.Host;

/// <summary>
/// OS-enforced same-host exclusion for one financial Host per account/runtime.
///
/// This is a concurrency prerequisite only. It does not prove remote/copy-source
/// process death, credential revocation, or recovery takeover authority. Those
/// remain separate external #696 evidence requirements.
/// </summary>
public sealed class HostLifetimeExclusiveLease : IDisposable
{
    private const string Domain = "autotrade-host-lifetime-fence-v1";
    private FileStream? _stream;

    private HostLifetimeExclusiveLease(FileStream stream, string scopeId)
    {
        _stream = stream;
        ScopeId = scopeId;
    }

    internal string ScopeId { get; }

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
        Directory.CreateDirectory(rootDirectory);
        string path = Path.Combine(rootDirectory, scopeId + ".lock");

        FileStream stream;
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
                "Another AutoTrade Host holds the same account/runtime lifetime fence.",
                error);
        }
        catch (UnauthorizedAccessException error)
        {
            throw new HostLifetimeFenceUnavailableException(
                "Host lifetime fence storage is not writable.",
                error);
        }

        try
        {
            WriteOwnerRecord(stream, options, scopeId);
            return new HostLifetimeExclusiveLease(stream, scopeId);
        }
        catch
        {
            stream.Dispose();
            throw;
        }
    }

    internal static string ScopeIdFor(HostProcessOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        byte[] material = Encoding.UTF8.GetBytes(
            Domain + "\0" + options.Environment + "\0" + options.AccountId);
        try
        {
            return "hf-" + Convert.ToHexString(SHA256.HashData(material)).ToLowerInvariant();
        }
        finally
        {
            CryptographicOperations.ZeroMemory(material);
        }
    }

    private static void WriteOwnerRecord(
        FileStream stream,
        HostProcessOptions options,
        string scopeId)
    {
        using Process process = Process.GetCurrentProcess();
        DateTimeOffset startedAt = process.StartTime.ToUniversalTime();
        byte[] payload = JsonSerializer.SerializeToUtf8Bytes(new
        {
            schema_version = 1,
            scope_id = scopeId,
            host_id = options.HostId,
            process_id = Environment.ProcessId,
            process_started_at = startedAt.ToString("O"),
        });
        try
        {
            stream.Position = 0;
            stream.SetLength(0);
            stream.Write(payload, 0, payload.Length);
            stream.Flush(flushToDisk: true);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(payload);
        }
    }

    public void Dispose()
    {
        FileStream? stream = Interlocked.Exchange(ref _stream, null);
        stream?.Dispose();
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
