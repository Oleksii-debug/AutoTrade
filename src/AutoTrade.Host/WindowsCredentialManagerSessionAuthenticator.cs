using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace AutoTrade.Host;

public sealed record AuthenticatedHostSession(string Actor, string PublicSessionReference);

/// <summary>
/// Resolves the paired UI session inside the host process from Windows Credential Manager.
/// Reusable bearer bytes are never returned to endpoint handlers, logs, responses, or durable state.
/// This is UI/host authentication only; it is not provider credential or financial-send authority.
/// </summary>
public sealed class WindowsCredentialManagerSessionAuthenticator
{
    private const uint CredentialTypeGeneric = 1;
    private const string CredentialTargetPrefix = "AutoTrade.HostSession:";
    private readonly string _credentialTarget;

    public WindowsCredentialManagerSessionAuthenticator(HostProcessOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        _credentialTarget = options.CredentialTarget;
    }

    public static string CredentialTargetForOrigin(Uri origin)
    {
        ArgumentNullException.ThrowIfNull(origin);
        if (!origin.IsAbsoluteUri
            || !string.IsNullOrEmpty(origin.UserInfo)
            || !string.IsNullOrEmpty(origin.Query)
            || !string.IsNullOrEmpty(origin.Fragment)
            || origin.AbsolutePath != "/")
        {
            throw new ArgumentException("Credential origin must be a canonical absolute origin.", nameof(origin));
        }
        return CredentialTargetPrefix + origin.GetLeftPart(UriPartial.Authority);
    }

    public static string PublicSessionReference(string token)
    {
        string canonical = Required(token, nameof(token));
        byte[] material = Encoding.UTF8.GetBytes("autotrade-ui-session-v1\0" + canonical);
        byte[] digest = [];
        try
        {
            digest = SHA256.HashData(material);
            return "sid-" + Convert.ToHexString(digest).ToLowerInvariant();
        }
        finally
        {
            CryptographicOperations.ZeroMemory(material);
            if (digest.Length != 0)
            {
                CryptographicOperations.ZeroMemory(digest);
            }
        }
    }

    public bool TryAuthenticate(HttpRequest request, out AuthenticatedHostSession? session)
    {
        ArgumentNullException.ThrowIfNull(request);
        session = null;
        if (!OperatingSystem.IsWindows())
        {
            return false;
        }

        if (!TryReadRequestIdentity(request, out string actor, out string token))
        {
            return false;
        }

        if (!TryReadCredential(out string expectedActor, out byte[] expectedTokenUtf8))
        {
            return false;
        }

        byte[] actualTokenUtf8 = Encoding.UTF8.GetBytes(token);
        try
        {
            bool tokenMatches = actualTokenUtf8.Length == expectedTokenUtf8.Length
                && CryptographicOperations.FixedTimeEquals(actualTokenUtf8, expectedTokenUtf8);
            if (!tokenMatches
                || !string.Equals(actor, expectedActor, StringComparison.Ordinal))
            {
                return false;
            }
            session = new AuthenticatedHostSession(
                actor,
                PublicSessionReference(token));
            return true;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(actualTokenUtf8);
            CryptographicOperations.ZeroMemory(expectedTokenUtf8);
        }
    }

    private static bool TryReadRequestIdentity(
        HttpRequest request,
        out string actor,
        out string token)
    {
        actor = string.Empty;
        token = string.Empty;
        string authorization = request.Headers.Authorization.ToString();
        const string Prefix = "AutoTrade-Session ";
        if (!authorization.StartsWith(Prefix, StringComparison.Ordinal)
            || authorization.Length <= Prefix.Length
            || authorization.Contains(",", StringComparison.Ordinal))
        {
            return false;
        }
        token = authorization[Prefix.Length..];
        if (string.IsNullOrWhiteSpace(token)
            || !string.Equals(token, token.Trim(), StringComparison.Ordinal))
        {
            return false;
        }

        if (!request.Headers.TryGetValue("X-AutoTrade-Actor", out var values)
            || values.Count != 1)
        {
            return false;
        }
        actor = values[0] ?? string.Empty;
        return !string.IsNullOrWhiteSpace(actor)
            && string.Equals(actor, actor.Trim(), StringComparison.Ordinal);
    }

    private bool TryReadCredential(out string actor, out byte[] tokenUtf8)
    {
        actor = string.Empty;
        tokenUtf8 = [];
        if (!CredRead(_credentialTarget, CredentialTypeGeneric, 0, out IntPtr pointer))
        {
            int error = Marshal.GetLastWin32Error();
            const int ErrorNotFound = 1168;
            if (error == ErrorNotFound)
            {
                return false;
            }
            throw new Win32Exception(error, "Paired AutoTrade host session could not be read.");
        }

        try
        {
            NativeCredential credential = Marshal.PtrToStructure<NativeCredential>(pointer);
            actor = (Marshal.PtrToStringUni(credential.UserName) ?? string.Empty).Trim();
            if (string.IsNullOrEmpty(actor)
                || credential.CredentialBlob == IntPtr.Zero
                || credential.CredentialBlobSize == 0
                || credential.CredentialBlobSize % 2 != 0
                || credential.CredentialBlobSize > 8192)
            {
                return false;
            }

            byte[] utf16 = new byte[checked((int)credential.CredentialBlobSize)];
            try
            {
                Marshal.Copy(credential.CredentialBlob, utf16, 0, utf16.Length);
                string token = Encoding.Unicode.GetString(utf16).TrimEnd('\0');
                if (string.IsNullOrWhiteSpace(token)
                    || !string.Equals(token, token.Trim(), StringComparison.Ordinal))
                {
                    return false;
                }
                tokenUtf8 = Encoding.UTF8.GetBytes(token);
                return true;
            }
            finally
            {
                CryptographicOperations.ZeroMemory(utf16);
            }
        }
        finally
        {
            CredFree(pointer);
        }
    }

    private static string Required(string value, string name)
    {
        if (string.IsNullOrWhiteSpace(value)
            || !string.Equals(value, value.Trim(), StringComparison.Ordinal))
        {
            throw new ArgumentException($"{name} must be canonical non-empty text.", name);
        }
        return value;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredential
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

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
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

    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("Advapi32.dll")]
    private static extern void CredFree(IntPtr credential);
}
