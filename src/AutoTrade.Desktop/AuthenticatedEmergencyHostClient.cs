using System.ComponentModel;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace AutoTrade.Desktop;

public sealed record EmergencyHostSession(string Actor, string Token)
{
    public EmergencyHostSession Validated()
    {
        if (string.IsNullOrWhiteSpace(Actor)
            || !string.Equals(Actor, Actor.Trim(), StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Emergency host session actor is invalid.");
        }

        if (string.IsNullOrWhiteSpace(Token)
            || !string.Equals(Token, Token.Trim(), StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Emergency host session token is invalid.");
        }

        return this;
    }
}

public interface IEmergencyHostSessionProvider
{
    EmergencyHostSession GetSession();
}

/// <summary>
/// Reads a pre-paired short-lived host session from the current user's Windows
/// Credential Manager. Target configuration is non-secret; the token never comes
/// from environment variables or command-line arguments.
/// </summary>
public sealed class WindowsCredentialManagerSessionProvider : IEmergencyHostSessionProvider
{
    private const uint CredentialTypeGeneric = 1;
    private readonly string _targetName;

    public WindowsCredentialManagerSessionProvider(string targetName)
    {
        if (string.IsNullOrWhiteSpace(targetName))
        {
            throw new ArgumentException("Credential target is required.", nameof(targetName));
        }

        _targetName = targetName.Trim();
    }

    public EmergencyHostSession GetSession()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "Windows Credential Manager is required for desktop host sessions.");
        }

        if (!CredRead(_targetName, CredentialTypeGeneric, 0, out IntPtr credentialPointer))
        {
            throw new Win32Exception(
                Marshal.GetLastWin32Error(),
                "The paired AutoTrade host session is unavailable.");
        }

        try
        {
            NativeCredential credential =
                Marshal.PtrToStructure<NativeCredential>(credentialPointer);
            string actor = Marshal.PtrToStringUni(credential.UserName)
                ?? throw new InvalidOperationException(
                    "The paired host credential has no actor identity.");

            if (credential.CredentialBlob == IntPtr.Zero
                || credential.CredentialBlobSize == 0
                || credential.CredentialBlobSize % 2 != 0
                || credential.CredentialBlobSize > 8192)
            {
                throw new InvalidOperationException(
                    "The paired host credential token is malformed.");
            }

            byte[] tokenBytes =
                new byte[checked((int)credential.CredentialBlobSize)];
            try
            {
                Marshal.Copy(
                    credential.CredentialBlob,
                    tokenBytes,
                    0,
                    checked((int)credential.CredentialBlobSize));
                string token = Encoding.Unicode.GetString(tokenBytes).TrimEnd('\0');
                return new EmergencyHostSession(actor.Trim(), token).Validated();
            }
            finally
            {
                CryptographicOperations.ZeroMemory(tokenBytes);
            }
        }
        finally
        {
            CredFree(credentialPointer);
        }
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

    [DllImport("Advapi32.dll")]
    private static extern void CredFree(IntPtr credential);
}

public sealed class EmergencyCommandUncertainException : Exception
{
    public string CommandId { get; }

    public EmergencyCommandUncertainException(string commandId, string message, Exception? inner = null)
        : base(message, inner)
    {
        if (!Guid.TryParseExact(commandId, "D", out _))
        {
            throw new ArgumentException(
                "Uncertain emergency command identity must be a canonical UUID.",
                nameof(commandId));
        }

        CommandId = commandId;
    }
}

/// <summary>
/// Authenticated client for the one canonical versioned host API. It contains no
/// provider credentials, financial logic, or alternate command authority.
/// </summary>
public sealed class AuthenticatedEmergencyHostClient : IEmergencyHostClient
{
    private readonly HttpClient _httpClient;
    private readonly IEmergencyHostSessionProvider _sessionProvider;
    private readonly SemaphoreSlim _commandGate = new(1, 1);
    private PendingCommand? _pendingCommand;

    public AuthenticatedEmergencyHostClient(
        HttpClient httpClient,
        Uri baseUri,
        IEmergencyHostSessionProvider sessionProvider)
    {
        _httpClient = httpClient ?? throw new ArgumentNullException(nameof(httpClient));
        _sessionProvider = sessionProvider
            ?? throw new ArgumentNullException(nameof(sessionProvider));
        BaseUri = ValidateBaseUri(baseUri);
        _httpClient.BaseAddress = BaseUri;
    }

    public Uri BaseUri { get; }

    public async Task<EmergencyHostStatus> GetStatusAsync(
        CancellationToken cancellationToken)
    {
        Snapshot snapshot = await GetSnapshotAsync(cancellationToken);
        return snapshot.Status;
    }

    public async Task<EmergencyCommandResult> BlockNewExposureAsync(
        CancellationToken cancellationToken)
    {
        await _commandGate.WaitAsync(cancellationToken);
        try
        {
            EmergencyHostSession currentSession = _sessionProvider.GetSession().Validated();
            bool recoveringUncertainCommand = _pendingCommand is not null;
            PendingCommand pending;

            if (_pendingCommand is { } existing)
            {
                if (!string.Equals(
                        existing.Actor,
                        currentSession.Actor,
                        StringComparison.Ordinal)
                    || !FixedTimeEquals(existing.Session, currentSession.Token))
                {
                    throw new EmergencyCommandUncertainException(
                        existing.CommandId,
                        "The unresolved emergency command is bound to a different or expired host session. "
                        + "Its identity is preserved and will not be retargeted.");
                }

                pending = existing;
            }
            else
            {
                Snapshot snapshot = await GetSnapshotAsync(
                    cancellationToken,
                    currentSession);
                pending = new PendingCommand(
                    CommandId: Guid.NewGuid().ToString("D"),
                    IdempotencyKey: Guid.NewGuid().ToString("D"),
                    Actor: currentSession.Actor,
                    Session: currentSession.Token,
                    AccountId: snapshot.Status.AccountId,
                    Environment: snapshot.Status.Environment,
                    ExpectedStateVersion: snapshot.Status.StateVersion);
                _pendingCommand = pending;
            }

            using HttpRequestMessage request = CreateRequest(
                HttpMethod.Post,
                "api/v1/commands",
                new EmergencyHostSession(pending.Actor, pending.Session));
            request.Content = new StringContent(
                JsonSerializer.Serialize(
                    new
                    {
                        command_id = pending.CommandId,
                        expected_state_version = pending.ExpectedStateVersion,
                        idempotency_key = pending.IdempotencyKey,
                        actor = pending.Actor,
                        session = pending.Session,
                        account_id = pending.AccountId,
                        environment = pending.Environment,
                        action = "BLOCK_NEW_EXPOSURE",
                        payload = new { },
                    }),
                Encoding.UTF8,
                "application/json");

            HttpResponseMessage response;
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                response = await _httpClient.SendAsync(
                    request,
                    HttpCompletionOption.ResponseHeadersRead,
                    cancellationToken);
            }
            catch (OperationCanceledException error)
            {
                throw new EmergencyCommandUncertainException(
                    pending.CommandId,
                    "The host command response was cancelled or timed out after sending began. "
                    + "The original command identity is retained because durable acceptance may already have occurred.",
                    error);
            }
            catch (HttpRequestException error)
            {
                throw new EmergencyCommandUncertainException(
                    pending.CommandId,
                    "The host command response was not confirmed. The original command identity is retained for exact recovery.",
                    error);
            }

            using (response)
            {
                if (response.StatusCode is HttpStatusCode.Unauthorized
                    or HttpStatusCode.Forbidden)
                {
                    if (!recoveringUncertainCommand)
                    {
                        _pendingCommand = null;
                    }
                    else
                    {
                        throw new EmergencyCommandUncertainException(
                            pending.CommandId,
                            "The unresolved command can no longer be authenticated with its original session. "
                            + "It remains unresolved and will not be replaced.");
                    }

                    return new EmergencyCommandResult(
                        accepted: false,
                        durableBlockConfirmed: false,
                        inFlightActions: InFlightActionState.Unknown,
                        operationId: "Unavailable",
                        message: "The authenticated host rejected this session before accepting the emergency command.");
                }

                if (response.StatusCode is not HttpStatusCode.OK
                    and not HttpStatusCode.Conflict)
                {
                    throw new EmergencyCommandUncertainException(
                        pending.CommandId,
                        $"Host command returned HTTP {(int)response.StatusCode} after the send began. "
                        + "Durable acceptance is not being inferred or denied; the exact command identity is retained.");
                }

                JsonElement result = await ReadObjectAsync(response, cancellationToken);
                string commandId = RequiredString(result, "command_id");
                if (!string.Equals(commandId, pending.CommandId, StringComparison.Ordinal))
                {
                    throw new EmergencyCommandUncertainException(
                        pending.CommandId,
                        "The host response command identity did not match the unresolved request.");
                }

                string status = RequiredString(result, "status");
                if (status == "ACCEPTED")
                {
                    string operationId = CanonicalGuid(
                        RequiredString(result, "operation_id"),
                        "operation_id");
                    _pendingCommand = null;
                    try
                    {
                        EmergencyOperationStatus operation = await GetOperationAsync(
                            operationId,
                            cancellationToken);
                        return new EmergencyCommandResult(
                            accepted: true,
                            durableBlockConfirmed: operation.DurableBlockConfirmed,
                            inFlightActions: operation.InFlightActions,
                            operationId: operationId,
                            message: "The host accepted the emergency command as operation "
                                + operationId + ".");
                    }
                    catch (OperationCanceledException)
                    {
                        throw;
                    }
                    catch
                    {
                        return new EmergencyCommandResult(
                            accepted: true,
                            durableBlockConfirmed: false,
                            inFlightActions: InFlightActionState.Unknown,
                            operationId: operationId,
                            message: "The host accepted the emergency command, but its current operation phase could not be recovered.");
                    }
                }

                if (status is "CONFLICT" or "REJECTED")
                {
                    if (!recoveringUncertainCommand)
                    {
                        _pendingCommand = null;
                    }
                    else
                    {
                        throw new EmergencyCommandUncertainException(
                            pending.CommandId,
                            "Exact recovery returned a conflict/rejection instead of the original durable result. "
                            + "The unresolved command identity is preserved.");
                    }

                    return new EmergencyCommandResult(
                        accepted: false,
                        durableBlockConfirmed: false,
                        inFlightActions: InFlightActionState.Unknown,
                        operationId: "Unavailable",
                        message: status == "CONFLICT"
                            ? "The host state changed before this emergency command was admitted. Refresh and review before a new request."
                            : "The host rejected the emergency command.");
                }

                throw new EmergencyCommandUncertainException(
                    pending.CommandId,
                    "The host returned a non-canonical command status. The command identity remains unresolved.");
            }
        }
        finally
        {
            _commandGate.Release();
        }
    }

    public async Task<EmergencyOperationStatus> GetOperationAsync(
        string operationId,
        CancellationToken cancellationToken)
    {
        string canonicalId = CanonicalGuid(operationId, nameof(operationId));
        EmergencyHostSession session = _sessionProvider.GetSession().Validated();
        using HttpRequestMessage request = CreateRequest(
            HttpMethod.Get,
            "api/v1/operations/" + Uri.EscapeDataString(canonicalId),
            session);
        using HttpResponseMessage response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken);
        response.EnsureSuccessStatusCode();

        JsonElement value = await ReadObjectAsync(response, cancellationToken);
        string returnedId = CanonicalGuid(
            RequiredString(value, "operation_id"),
            "operation_id");
        if (!string.Equals(returnedId, canonicalId, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "Host operation response identity does not match the requested operation.");
        }

        string phase = RequiredString(value, "phase");
        string[] uncertainty = RequiredStringArray(value, "remaining_uncertainty");
        string uncertaintyText = uncertainty.Length == 0
            ? "Host reports no operation-level uncertainty; outstanding provider in-flight actions are not asserted by this operation."
            : string.Join("; ", uncertainty);

        EmergencyOperationState state = phase switch
        {
            "QUEUED" => EmergencyOperationState.Accepted,
            "RUNNING" or "WAITING_EXTERNAL" => EmergencyOperationState.Running,
            "SUCCEEDED" => EmergencyOperationState.Succeeded,
            "FAILED" => EmergencyOperationState.Failed,
            "CANCELLED" => EmergencyOperationState.Cancelled,
            "UNKNOWN" => EmergencyOperationState.Unknown,
            _ => throw new InvalidOperationException(
                "Host returned a non-canonical operation phase."),
        };

        bool durableBlock = state == EmergencyOperationState.Succeeded;
        return new EmergencyOperationStatus(
            operationId: returnedId,
            state: state,
            durableBlockConfirmed: durableBlock,
            inFlightActions: InFlightActionState.Unknown,
            message: phase switch
            {
                "SUCCEEDED" => "The host reports that the block-new-exposure operation succeeded.",
                "FAILED" => "The host reports that the block-new-exposure operation failed.",
                "CANCELLED" => "The host reports that the block-new-exposure operation was cancelled.",
                "UNKNOWN" => "The host cannot currently determine the operation outcome.",
                _ => "The host reports that the block-new-exposure operation is still in progress.",
            },
            remainingUncertainty: uncertaintyText);
    }

    private async Task<Snapshot> GetSnapshotAsync(
        CancellationToken cancellationToken,
        EmergencyHostSession? knownSession = null)
    {
        EmergencyHostSession session =
            (knownSession ?? _sessionProvider.GetSession()).Validated();
        using HttpRequestMessage request = CreateRequest(
            HttpMethod.Get,
            "api/v1/state",
            session);
        using HttpResponseMessage response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken);
        response.EnsureSuccessStatusCode();

        JsonElement value = await ReadObjectAsync(response, cancellationToken);
        string hostId = RequiredString(value, "host_id");
        string accountId = RequiredString(value, "account_id");
        string environment = RequiredString(value, "environment");
        string stateVersion = CanonicalSequence(
            RequiredString(value, "state_version"),
            "state_version");
        string eventCursor = CanonicalSequence(
            RequiredString(value, "event_cursor"),
            "event_cursor");
        DateTimeOffset observed = RequiredUtcInstant(value, "server_time");

        if (ContainsSecret(value, session.Token))
        {
            throw new InvalidOperationException(
                "Host snapshot must not echo the reusable session credential.");
        }

        JsonElement permissions = RequiredObject(value, "permission_summary");
        if (permissions.TryGetProperty("session", out _))
        {
            throw new InvalidOperationException(
                "Host snapshot permission metadata must not expose a reusable session credential.");
        }

        if (!string.Equals(
                RequiredString(permissions, "actor"),
                session.Actor,
                StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "Host snapshot authenticated actor does not match the local paired session.");
        }

        string role = RequiredString(permissions, "role");
        if (role is not ("OWNER" or "OPERATOR" or "RESEARCHER" or "OBSERVER"))
        {
            throw new InvalidOperationException(
                "Host snapshot permission role is not canonical.");
        }

        JsonElement freshness = RequiredObject(value, "connection_freshness");
        if (!freshness.EnumerateObject().Any())
        {
            throw new InvalidOperationException(
                "Host snapshot has no connection freshness evidence.");
        }

        EmergencyHostStatus status = new EmergencyHostStatus(
            Connected: true,
            HostId: hostId,
            AccountId: accountId,
            Environment: environment,
            StateVersion: stateVersion,
            ObservedAtUtc: observed,
            Message: "Authenticated host state was refreshed from canonical version "
                + stateVersion + ".").Validated();
        return new Snapshot(status, eventCursor);
    }

    private HttpRequestMessage CreateRequest(
        HttpMethod method,
        string relativePath,
        EmergencyHostSession session)
    {
        HttpRequestMessage request = new(method, new Uri(BaseUri, relativePath));
        request.Headers.Accept.Add(
            new MediaTypeWithQualityHeaderValue("application/json"));
        request.Headers.Authorization =
            new AuthenticationHeaderValue("AutoTrade-Session", session.Token);
        request.Headers.Add("X-AutoTrade-Actor", session.Actor);
        request.Headers.CacheControl = new CacheControlHeaderValue { NoStore = true };
        return request;
    }

    private static async Task<JsonElement> ReadObjectAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        await using Stream stream = await response.Content.ReadAsStreamAsync(
            cancellationToken);
        using JsonDocument document = await JsonDocument.ParseAsync(
            stream,
            cancellationToken: cancellationToken);
        if (document.RootElement.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Host response must be a JSON object.");
        }

        return document.RootElement.Clone();
    }

    private static JsonElement RequiredObject(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out JsonElement property)
            || property.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException(
                $"Host response {name} must be an object.");
        }

        return property;
    }

    private static bool ContainsSecret(JsonElement value, string secret)
    {
        if (string.IsNullOrEmpty(secret))
        {
            return false;
        }

        return value.ValueKind switch
        {
            JsonValueKind.String =>
                (value.GetString() ?? string.Empty).Contains(
                    secret,
                    StringComparison.Ordinal),
            JsonValueKind.Object => value.EnumerateObject().Any(
                property => property.Name.Contains(secret, StringComparison.Ordinal)
                    || ContainsSecret(property.Value, secret)),
            JsonValueKind.Array => value.EnumerateArray().Any(
                item => ContainsSecret(item, secret)),
            _ => false,
        };
    }

    private static string RequiredString(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out JsonElement property)
            || property.ValueKind != JsonValueKind.String
            || string.IsNullOrWhiteSpace(property.GetString()))
        {
            throw new InvalidOperationException(
                $"Host response {name} must be a non-empty string.");
        }

        string result = property.GetString()!;
        if (!string.Equals(result, result.Trim(), StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                $"Host response {name} is not canonical.");
        }

        return result;
    }

    private static string[] RequiredStringArray(JsonElement value, string name)
    {
        if (!value.TryGetProperty(name, out JsonElement property)
            || property.ValueKind != JsonValueKind.Array)
        {
            throw new InvalidOperationException(
                $"Host response {name} must be an array.");
        }

        List<string> values = [];
        foreach (JsonElement item in property.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(item.GetString()))
            {
                throw new InvalidOperationException(
                    $"Host response {name} contains an invalid item.");
            }

            values.Add(item.GetString()!);
        }

        return values.ToArray();
    }

    private static DateTimeOffset RequiredUtcInstant(JsonElement value, string name)
    {
        string token = RequiredString(value, name);
        if (!token.EndsWith('Z')
            || !DateTimeOffset.TryParse(
                token,
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AssumeUniversal
                    | System.Globalization.DateTimeStyles.AdjustToUniversal,
                out DateTimeOffset parsed)
            || parsed.Offset != TimeSpan.Zero)
        {
            throw new InvalidOperationException(
                $"Host response {name} must be a UTC instant.");
        }

        return parsed;
    }

    private static string CanonicalGuid(string value, string name)
    {
        if (!Guid.TryParseExact(value, "D", out Guid parsed)
            || !string.Equals(
                value,
                parsed.ToString("D"),
                StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                $"{name} must be a canonical UUID.");
        }

        return value;
    }

    private static string CanonicalSequence(string value, string name)
    {
        bool valid = value == "0";
        if (!valid && value.Length > 0 && value[0] is >= '1' and <= '9')
        {
            valid = value.All(character => character is >= '0' and <= '9');
        }

        if (!valid)
        {
            throw new InvalidOperationException(
                $"{name} must be a canonical non-negative integer.");
        }

        return value;
    }

    private static bool FixedTimeEquals(string left, string right)
    {
        byte[] leftBytes = Encoding.UTF8.GetBytes(left);
        byte[] rightBytes = Encoding.UTF8.GetBytes(right);
        try
        {
            return leftBytes.Length == rightBytes.Length
                && CryptographicOperations.FixedTimeEquals(leftBytes, rightBytes);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(leftBytes);
            CryptographicOperations.ZeroMemory(rightBytes);
        }
    }

    private static Uri ValidateBaseUri(Uri value)
    {
        if (value is null || !value.IsAbsoluteUri)
        {
            throw new ArgumentException("Host base URI must be absolute.", nameof(value));
        }

        if (!string.IsNullOrEmpty(value.UserInfo)
            || !string.IsNullOrEmpty(value.Query)
            || !string.IsNullOrEmpty(value.Fragment)
            || value.AbsolutePath != "/")
        {
            throw new ArgumentException(
                "Host base URI must be an origin without user info, path, query, or fragment.",
                nameof(value));
        }

        bool secure = value.Scheme == Uri.UriSchemeHttps;
        bool loopbackHttp = value.Scheme == Uri.UriSchemeHttp
            && (string.Equals(value.Host, "localhost", StringComparison.OrdinalIgnoreCase)
                || (IPAddress.TryParse(value.Host, out IPAddress? address)
                    && IPAddress.IsLoopback(address)));
        if (!secure && !loopbackHttp)
        {
            throw new ArgumentException(
                "Host base URI must use HTTPS or loopback HTTP.",
                nameof(value));
        }

        return value;
    }

    private sealed record Snapshot(
        EmergencyHostStatus Status,
        string EventCursor);

    private sealed record PendingCommand(
        string CommandId,
        string IdempotencyKey,
        string Actor,
        string Session,
        string AccountId,
        string Environment,
        string ExpectedStateVersion);
}

internal static class DesktopHostClientFactory
{
    public static IEmergencyHostClient Create()
    {
        string? uriText = Environment.GetEnvironmentVariable("AUTOTRADE_HOST_URI");
        string? credentialTarget =
            Environment.GetEnvironmentVariable("AUTOTRADE_HOST_CREDENTIAL_TARGET");
        if (string.IsNullOrWhiteSpace(uriText)
            || string.IsNullOrWhiteSpace(credentialTarget)
            || !Uri.TryCreate(uriText.Trim(), UriKind.Absolute, out Uri? uri))
        {
            return new DisconnectedEmergencyHostClient(
                "Authenticated host connection is not configured. "
                + "Set the non-secret host URI and Windows Credential Manager target after pairing.");
        }

        try
        {
            HttpClientHandler handler = new()
            {
                AllowAutoRedirect = false,
                UseCookies = false,
            };
            HttpClient httpClient = new(handler)
            {
                Timeout = TimeSpan.FromSeconds(10),
            };
            return new AuthenticatedEmergencyHostClient(
                httpClient,
                uri,
                new WindowsCredentialManagerSessionProvider(
                    credentialTarget.Trim()));
        }
        catch (Exception error) when (
            error is ArgumentException
            or InvalidOperationException
            or PlatformNotSupportedException)
        {
            return new DisconnectedEmergencyHostClient(
                "Authenticated host configuration is invalid. "
                + "No durable emergency command can be issued until pairing/configuration is repaired.");
        }
    }
}
