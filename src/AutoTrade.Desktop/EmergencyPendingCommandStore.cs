using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace AutoTrade.Desktop;

public interface IEmergencyPendingCommandStore
{
    string? Load();
    void Save(string payload);
    void Clear();
}

internal sealed class VolatileEmergencyPendingCommandStore : IEmergencyPendingCommandStore
{
    private string? _payload;

    public string? Load() => _payload;

    public void Save(string payload)
    {
        if (string.IsNullOrWhiteSpace(payload))
        {
            throw new ArgumentException(
                "Pending emergency command payload is required.",
                nameof(payload));
        }

        _payload = payload;
    }

    public void Clear() => _payload = null;
}

/// <summary>
/// Persists the one unresolved emergency command in the current user's Windows
/// Credential Manager. The payload contains the original short-lived session
/// credential and therefore must never be written to a normal configuration file.
/// </summary>
public sealed class WindowsCredentialManagerPendingCommandStore
    : IEmergencyPendingCommandStore
{
    private const uint CredentialTypeGeneric = 1;
    private const uint CredentialPersistLocalMachine = 2;
    private const int ErrorNotFound = 1168;
    private const int MaxPayloadBytes = 4096;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    private readonly string _targetName;

    public WindowsCredentialManagerPendingCommandStore(string targetName)
    {
        if (string.IsNullOrWhiteSpace(targetName)
            || !string.Equals(
                targetName,
                targetName.Trim(),
                StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Pending command credential target is invalid.",
                nameof(targetName));
        }

        _targetName = targetName;
    }

    public string? Load()
    {
        EnsureWindows();
        if (!CredRead(
                _targetName,
                CredentialTypeGeneric,
                0,
                out IntPtr credentialPointer))
        {
            int error = Marshal.GetLastWin32Error();
            if (error == ErrorNotFound)
            {
                return null;
            }

            throw new Win32Exception(
                error,
                "The secure pending emergency command could not be read.");
        }

        try
        {
            NativeCredentialRead credential =
                Marshal.PtrToStructure<NativeCredentialRead>(
                    credentialPointer);
            if (credential.Type != CredentialTypeGeneric
                || credential.CredentialBlob == IntPtr.Zero
                || credential.CredentialBlobSize == 0
                || credential.CredentialBlobSize > MaxPayloadBytes)
            {
                throw new InvalidOperationException(
                    "The secure pending emergency command is malformed.");
            }

            byte[] bytes = new byte[checked((int)credential.CredentialBlobSize)];
            try
            {
                Marshal.Copy(
                    credential.CredentialBlob,
                    bytes,
                    0,
                    bytes.Length);
                string payload = StrictUtf8.GetString(bytes);
                if (string.IsNullOrWhiteSpace(payload))
                {
                    throw new InvalidOperationException(
                        "The secure pending emergency command is empty.");
                }

                return payload;
            }
            finally
            {
                CryptographicOperations.ZeroMemory(bytes);
            }
        }
        finally
        {
            CredFree(credentialPointer);
        }
    }

    public void Save(string payload)
    {
        EnsureWindows();
        if (string.IsNullOrWhiteSpace(payload))
        {
            throw new ArgumentException(
                "Pending emergency command payload is required.",
                nameof(payload));
        }

        byte[] bytes = StrictUtf8.GetBytes(payload);
        if (bytes.Length == 0 || bytes.Length > MaxPayloadBytes)
        {
            CryptographicOperations.ZeroMemory(bytes);
            throw new InvalidOperationException(
                "Pending emergency command is too large for the secure store.");
        }

        GCHandle handle = default;
        try
        {
            handle = GCHandle.Alloc(bytes, GCHandleType.Pinned);
            NativeCredentialWrite credential = new()
            {
                Type = CredentialTypeGeneric,
                TargetName = _targetName,
                Comment = "AutoTrade unresolved emergency command",
                CredentialBlobSize = checked((uint)bytes.Length),
                CredentialBlob = handle.AddrOfPinnedObject(),
                Persist = CredentialPersistLocalMachine,
                UserName = "AutoTrade",
            };

            if (!CredWrite(ref credential, 0))
            {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "The secure pending emergency command could not be written.");
            }
        }
        finally
        {
            if (handle.IsAllocated)
            {
                handle.Free();
            }

            CryptographicOperations.ZeroMemory(bytes);
        }
    }

    public void Clear()
    {
        EnsureWindows();
        if (CredDelete(_targetName, CredentialTypeGeneric, 0))
        {
            return;
        }

        int error = Marshal.GetLastWin32Error();
        if (error != ErrorNotFound)
        {
            throw new Win32Exception(
                error,
                "The secure pending emergency command could not be cleared.");
        }
    }

    private static void EnsureWindows()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Windows Credential Manager is required for pending emergency command recovery.");
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredentialRead
    {
        public uint Flags;
        public uint Type;
        public IntPtr TargetName;
        public IntPtr Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public IntPtr TargetAlias;
        public IntPtr UserName;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredentialWrite
    {
        public uint Flags;
        public uint Type;
        [MarshalAs(UnmanagedType.LPWStr)]
        public string? TargetName;
        [MarshalAs(UnmanagedType.LPWStr)]
        public string? Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        [MarshalAs(UnmanagedType.LPWStr)]
        public string? TargetAlias;
        [MarshalAs(UnmanagedType.LPWStr)]
        public string? UserName;
    }

    [DllImport(
        "Advapi32.dll",
        EntryPoint = "CredReadW",
        CharSet = CharSet.Unicode,
        SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredRead(
        string target,
        uint type,
        uint flags,
        out IntPtr credential);

    [DllImport(
        "Advapi32.dll",
        EntryPoint = "CredWriteW",
        CharSet = CharSet.Unicode,
        SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredWrite(
        ref NativeCredentialWrite credential,
        uint flags);

    [DllImport(
        "Advapi32.dll",
        EntryPoint = "CredDeleteW",
        CharSet = CharSet.Unicode,
        SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredDelete(
        string target,
        uint type,
        uint flags);

    [DllImport("Advapi32.dll")]
    private static extern void CredFree(IntPtr credential);
}
