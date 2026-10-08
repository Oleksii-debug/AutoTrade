using AutoTrade.Contracts;
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

public sealed record EmergencyHostSession(string Actor, string Token, Uri Origin)
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

        Uri canonicalOrigin = AuthenticatedEmergencyHostClient.ValidateBaseUri(Origin);
        return this with
        {
            Actor = Actor.Trim(),
            Origin = canonicalOrigin,
        };
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
    private const string CredentialTargetPrefix = "AutoTrade.HostSession:";
    private readonly string _targetName;
    private readonly Uri _expectedOrigin;

    public WindowsCredentialManagerSessionProvider(
        string targetName,
        Uri expectedOrigin)
    {
        if (string.IsNullOrWhiteSpace(targetName))
        {
            throw new ArgumentException("Credential target is required.", nameof(targetName));
        }

        _expectedOrigin = AuthenticatedEmergencyHostClient.ValidateBaseUri(expectedOrigin);
        string canonicalTarget = CredentialTargetForOrigin(_expectedOrigin);
        if (!string.Equals(
                targetName.Trim(),
                canonicalTarget,
                StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Credential target is not bound to the configured paired host origin. "
                + "Re-pair the host instead of reusing a credential across origins.",
                nameof(targetName));
        }

        _targetName = canonicalTarget;
    }

    public static string CredentialTargetForOrigin(Uri origin)
    {
        Uri canonical = AuthenticatedEmergencyHostClient.ValidateBaseUri(origin);
        string authority = canonical.GetLeftPart(UriPartial.Authority);
        return CredentialTargetPrefix + authority;
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
                return new EmergencyHostSession(
                    actor.Trim(),
                    token,
                    _expectedOrigin).Validated();
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
/// The authenticated host is reachable but cannot currently expose one
/// coherent journal-cut snapshot. No durable state is carried by this exception.
/// </summary>
public sealed class EmergencySnapshotBusyException : Exception
{
    public EmergencySnapshotBusyException()
        : base(
            "The authenticated host is reachable, but one coherent state snapshot "
            + "is temporarily unavailable. Retry without treating prior state as current.")
    {
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
    private readonly IEmergencyPendingCommandStore _pendingCommandStore;
    private readonly SemaphoreSlim _commandGate = new(1, 1);
    private PendingCommand? _pendingCommand;

    public AuthenticatedEmergencyHostClient(
        HttpClient httpClient,
        Uri baseUri,
        IEmergencyHostSessionProvider sessionProvider,
        IEmergencyPendingCommandStore? pendingCommandStore = null)
    {
        _httpClient = httpClient ?? throw new ArgumentNullException(nameof(httpClient));
        _sessionProvider = sessionProvider
            ?? throw new ArgumentNullException(nameof(sessionProvider));
        _pendingCommandStore = pendingCommandStore
            ?? new VolatileEmergencyPendingCommandStore();
        BaseUri = ValidateBaseUri(baseUri);
        _httpClient.BaseAddress = BaseUri;
        _pendingCommand = LoadPendingCommand();
    }

    public Uri BaseUri { get; }

    private EmergencyHostSession GetBoundSession(
        EmergencyHostSession? knownSession = null)
    {
        EmergencyHostSession session =
            (knownSession ?? _sessionProvider.GetSession()).Validated();
        if (Uri.Compare(
                session.Origin,
                BaseUri,
                UriComponents.SchemeAndServer,
                UriFormat.UriEscaped,
                StringComparison.OrdinalIgnoreCase) != 0)
        {
            throw new InvalidOperationException(
                "The paired host session origin does not match the configured host origin. "
                + "No authenticated request was created.");
        }

        return session;
    }

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
            EmergencyHostSession currentSession = GetBoundSession();
            string currentSessionReference =
                PublicSessionReference(currentSession.Token);
            bool recoveringUncertainCommand = _pendingCommand is not null;
            PendingCommand pending;

            if (_pendingCommand is { } existing)
            {
                if (!string.Equals(
                        existing.Actor,
                        currentSession.Actor,
                        StringComparison.Ordinal))
                {
                    throw new EmergencyCommandUncertainException(
                        existing.CommandId,
                        "The unresolved emergency command is bound to a different actor. "
                        + "Its identity is preserved and will not be retargeted.");
                }

                if (!FixedTimeEquals(
                        existing.SessionReference,
                        currentSessionReference))
                {
                    return await RecoverAcceptedOperationAfterSessionRotationAsync(
                        existing,
                        currentSession,
                        cancellationToken);
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
                    SessionReference: currentSessionReference,
                    AccountId: snapshot.Status.AccountId,
                    Environment: snapshot.Status.Environment,
                    HostId: snapshot.Status.HostId,
                    ExpectedStateVersion: snapshot.Status.StateVersion);
                PersistPendingCommand(pending);
            }

            using HttpRequestMessage request = CreateRequest(
                HttpMethod.Post,
                HostApiRoutes.SubmitCommand,
                currentSession);
            request.Content = new StringContent(
                JsonSerializer.Serialize(
                    new
                    {
                        command_id = pending.CommandId,
                        expected_state_version = pending.ExpectedStateVersion,
                        idempotency_key = pending.IdempotencyKey,
                        actor = pending.Actor,
                        session = pending.SessionReference,
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
                        ClearPendingCommandOrThrow();
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
                    string expectedOperationId = HostOperationIdentity.Derive(
                        pending.AccountId,
                        pending.Environment,
                        pending.CommandId);
                    if (!string.Equals(
                            operationId,
                            expectedOperationId,
                            StringComparison.Ordinal))
                    {
                        throw new EmergencyCommandUncertainException(
                            pending.CommandId,
                            "The host response operation identity was not derived from the unresolved command scope.");
                    }
                    try
                    {
                        EmergencyOperationStatus operation = await GetOperationAsync(
                            operationId,
                            cancellationToken,
                            currentSession);
                        bool terminal = operation.State is
                            EmergencyOperationState.Succeeded
                            or EmergencyOperationState.Failed
                            or EmergencyOperationState.Cancelled;
                        bool recoveryRecordCleared =
                            terminal && TryClearPendingCommand();
                        return new EmergencyCommandResult(
                            accepted: true,
                            durableBlockConfirmed: operation.DurableBlockConfirmed,
                            inFlightActions: operation.InFlightActions,
                            operationId: operationId,
                            message: "The host accepted the emergency command as operation "
                                + operationId
                                + (terminal
                                    ? (recoveryRecordCleared
                                        ? "."
                                        : ". The terminal operation was observed, but the secure local recovery record could not be cleared.")
                                    : ". The operation is not terminal; the secure local recovery record is retained so a restart can recover this exact command before any new command is created."));
                    }
                    catch (OperationCanceledException)
                    {
                        // Acceptance was already observed. Keep the persisted exact
                        // command identity so restart recovery cannot mint a new
                        // emergency command while the accepted operation is unresolved.
                        throw;
                    }
                    catch
                    {
                        return new EmergencyCommandResult(
                            accepted: true,
                            durableBlockConfirmed: false,
                            inFlightActions: InFlightActionState.Unknown,
                            operationId: operationId,
                            message: "The host accepted the emergency command, but its current operation phase could not be recovered. "
                                + "The secure local recovery record remains and will force exact command recovery before any new command.");
                    }
                }

                if (status is "CONFLICT" or "REJECTED")
                {
                    if (!recoveringUncertainCommand)
                    {
                        ClearPendingCommandOrThrow();
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

    private async Task<EmergencyCommandResult> RecoverAcceptedOperationAfterSessionRotationAsync(
        PendingCommand pending,
        EmergencyHostSession currentSession,
        CancellationToken cancellationToken)
    {
        if (pending.HostId is null)
        {
            throw new EmergencyCommandUncertainException(
                pending.CommandId,
                "The unresolved emergency command predates durable host-identity binding. "
                + "A replacement session cannot prove that it is attached to the same host, so "
                + "the command remains unresolved and will not be resent or retargeted.");
        }

        Snapshot snapshot;
        try
        {
            snapshot = await GetSnapshotAsync(
                cancellationToken,
                currentSession);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception error)
        {
            throw new EmergencyCommandUncertainException(
                pending.CommandId,
                "The unresolved emergency command is bound to an earlier session, and the current session "
                + "could not prove the same authenticated host scope. The command remains unresolved and "
                + "will not be resent or retargeted.",
                error);
        }

        if (!string.Equals(
                snapshot.Status.HostId,
                pending.HostId,
                StringComparison.Ordinal)
            || !string.Equals(
                snapshot.Status.AccountId,
                pending.AccountId,
                StringComparison.Ordinal)
            || !string.Equals(
                snapshot.Status.Environment,
                pending.Environment,
                StringComparison.Ordinal))
        {
            throw new EmergencyCommandUncertainException(
                pending.CommandId,
                "The current authenticated host scope does not match the unresolved emergency command. "
                + "The command remains unresolved and will not be resent or retargeted.");
        }

        string operationId = HostOperationIdentity.Derive(
            pending.AccountId,
            pending.Environment,
            pending.CommandId);
        EmergencyOperationStatus operation;
        try
        {
            operation = await GetOperationAsync(
                operationId,
                cancellationToken,
                currentSession);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception error)
        {
            throw new EmergencyCommandUncertainException(
                pending.CommandId,
                "The current authenticated host did not prove the exact durable operation derived from "
                + "the unresolved command. The command remains unresolved and will not be resent or retargeted.",
                error);
        }

        bool terminal = operation.State is
            EmergencyOperationState.Succeeded
            or EmergencyOperationState.Failed
            or EmergencyOperationState.Cancelled;
        bool recoveryRecordCleared =
            terminal && TryClearPendingCommand();
        return new EmergencyCommandResult(
            accepted: true,
            durableBlockConfirmed: operation.DurableBlockConfirmed,
            inFlightActions: operation.InFlightActions,
            operationId: operationId,
            message: "A replacement authenticated session recovered the exact durable operation "
                + operationId
                + " by read-only identity lookup; the original command was not resent or retargeted"
                + (terminal
                    ? (recoveryRecordCleared
                        ? "."
                        : ". The terminal operation was observed, but the secure local recovery record could not be cleared.")
                    : ". The operation is not terminal; the secure local recovery record is retained."));
    }

    private void PersistPendingCommand(PendingCommand pending)
    {
        _pendingCommandStore.Save(SerializePendingCommand(pending));
        _pendingCommand = pending;
    }

    private static string SerializePendingCommand(PendingCommand pending)
    {
        Dictionary<string, string> payload = new(StringComparer.Ordinal)
        {
            ["schema_version"] = pending.HostId is null ? "2" : "3",
            ["command_id"] = pending.CommandId,
            ["idempotency_key"] = pending.IdempotencyKey,
            ["actor"] = pending.Actor,
            ["session"] = pending.SessionReference,
            ["account_id"] = pending.AccountId,
            ["environment"] = pending.Environment,
            ["expected_state_version"] = pending.ExpectedStateVersion,
        };
        if (pending.HostId is not null)
        {
            payload["host_id"] = pending.HostId;
        }

        return JsonSerializer.Serialize(payload);
    }

    private PendingCommand? LoadPendingCommand()
    {
        string? payload = _pendingCommandStore.Load();
        if (payload is null)
        {
            return null;
        }

        JsonElement value;
        try
        {
            using JsonDocument document = JsonDocument.Parse(payload);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidOperationException(
                    "Persisted emergency command must be a JSON object.");
            }

            value = document.RootElement.Clone();
        }
        catch (JsonException error)
        {
            throw new InvalidOperationException(
                "Persisted emergency command is not valid JSON.",
                error);
        }

        string schemaVersion = RequiredString(value, "schema_version");
        if (schemaVersion is not ("1" or "2" or "3"))
        {
            throw new InvalidOperationException(
                "Persisted emergency command schema version is unsupported.");
        }

        string[] expectedFields = schemaVersion == "3"
            ?
            [
                "schema_version",
                "command_id",
                "idempotency_key",
                "actor",
                "session",
                "account_id",
                "environment",
                "host_id",
                "expected_state_version",
            ]
            :
            [
                "schema_version",
                "command_id",
                "idempotency_key",
                "actor",
                "session",
                "account_id",
                "environment",
                "expected_state_version",
            ];
        string[] actualFields = value.EnumerateObject()
            .Select(property => property.Name)
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        string[] canonicalFields = expectedFields
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (!actualFields.SequenceEqual(canonicalFields, StringComparer.Ordinal))
        {
            throw new InvalidOperationException(
                "Persisted emergency command has an unexpected schema.");
        }

        string storedSession = RequiredString(value, "session");
        string sessionReference = schemaVersion == "1"
            ? PublicSessionReference(storedSession)
            : CanonicalSessionReference(storedSession, "session");

        PendingCommand pending = new(
            CommandId: CanonicalGuid(
                RequiredString(value, "command_id"),
                "command_id"),
            IdempotencyKey: CanonicalGuid(
                RequiredString(value, "idempotency_key"),
                "idempotency_key"),
            Actor: RequiredString(value, "actor"),
            SessionReference: sessionReference,
            AccountId: RequiredString(value, "account_id"),
            Environment: RequiredString(value, "environment"),
            HostId: schemaVersion == "3"
                ? RequiredString(value, "host_id")
                : null,
            ExpectedStateVersion: CanonicalSequence(
                RequiredString(value, "expected_state_version"),
                "expected_state_version"));

        if (schemaVersion == "1")
        {
            // V1 persisted the reusable bearer. Rewrite the same unresolved
            // command identity immediately to the bearer-free v2 record. Host
            // identity is intentionally not invented for legacy unresolved work.
            _pendingCommandStore.Save(SerializePendingCommand(pending));
        }

        return pending;
    }

    private bool TryClearPendingCommand()
    {
        try
        {
            _pendingCommandStore.Clear();
            _pendingCommand = null;
            return true;
        }
        catch
        {
            return false;
        }
    }

    private void ClearPendingCommandOrThrow()
    {
        _pendingCommandStore.Clear();
        _pendingCommand = null;
    }

    public Task<EmergencyOperationStatus> GetOperationAsync(
        string operationId,
        CancellationToken cancellationToken) =>
        GetOperationAsync(operationId, cancellationToken, knownSession: null);

    private async Task<EmergencyOperationStatus> GetOperationAsync(
        string operationId,
        CancellationToken cancellationToken,
        EmergencyHostSession? knownSession)
    {
        string canonicalId = CanonicalGuid(operationId, nameof(operationId));
        EmergencyHostSession session = GetBoundSession(knownSession);
        using HttpRequestMessage request = CreateRequest(
            HttpMethod.Get,
            HostApiRoutes.GetOperation(canonicalId),
            session);
        using HttpResponseMessage response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken);
        if (response.StatusCode == HttpStatusCode.ServiceUnavailable)
        {
            JsonElement unavailable = await ReadObjectAsync(
                response,
                cancellationToken);
            bool exactSnapshotBusy =
                unavailable.EnumerateObject().Count() == 2
                && unavailable.TryGetProperty("error", out JsonElement error)
                && error.ValueKind == JsonValueKind.String
                && string.Equals(
                    error.GetString(),
                    "SNAPSHOT_BUSY",
                    StringComparison.Ordinal)
                && unavailable.TryGetProperty(
                    "retryable",
                    out JsonElement retryable)
                && retryable.ValueKind == JsonValueKind.True;
            if (exactSnapshotBusy)
            {
                throw new EmergencySnapshotBusyException();
            }

            response.EnsureSuccessStatusCode();
        }
        else
        {
            response.EnsureSuccessStatusCode();
        }

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
        EmergencyHostSession session = GetBoundSession(knownSession);
        using HttpRequestMessage request = CreateRequest(
            HttpMethod.Get,
            HostApiRoutes.GetState,
            session);
        using HttpResponseMessage response = await _httpClient.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken);
        if (response.StatusCode == HttpStatusCode.ServiceUnavailable)
        {
            JsonElement unavailable = await ReadObjectAsync(response, cancellationToken);
            if (unavailable.EnumerateObject().Count() == 2
                && unavailable.TryGetProperty("error", out JsonElement error)
                && error.ValueKind == JsonValueKind.String
                && string.Equals(error.GetString(), "SNAPSHOT_BUSY", StringComparison.Ordinal)
                && unavailable.TryGetProperty("retryable", out JsonElement retryable)
                && retryable.ValueKind == JsonValueKind.True)
            {
                throw new EmergencySnapshotBusyException();
            }
        }
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
        if (!string.Equals(stateVersion, eventCursor, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "Host snapshot state_version and event_cursor must identify the same canonical host state.");
        }
        DateTimeOffset serverObservedAt =
            RequiredUtcInstant(value, "server_time");

        if (ContainsSecret(value, session.Token))
        {
            throw new InvalidOperationException(
                "Host snapshot must not echo the reusable session credential.");
        }

        JsonElement permissions = RequiredObject(value, "permission_summary");
        string sessionReference = CanonicalSessionReference(
            RequiredString(permissions, "session"),
            "permission_summary.session");
        if (!FixedTimeEquals(
                sessionReference,
                PublicSessionReference(session.Token)))
        {
            throw new InvalidOperationException(
                "Host snapshot session reference does not match the authenticated local session.");
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

        string hostFreshness = RequiredString(freshness, "host");
        if (hostFreshness is not ("CURRENT" or "STALE"))
        {
            throw new InvalidOperationException(
                "Host snapshot connection_freshness.host is not canonical.");
        }
        DateTimeOffset freshnessObservedAt =
            RequiredUtcInstant(freshness, "as_of");
        bool isCurrent = string.Equals(
            hostFreshness,
            "CURRENT",
            StringComparison.Ordinal);
        if (freshnessObservedAt > serverObservedAt)
        {
            throw new InvalidOperationException(
                "Host freshness evidence cannot be later than host server time.");
        }

        EmergencyHostStatus status = new EmergencyHostStatus(
            Connected: true,
            HostId: hostId,
            AccountId: accountId,
            Environment: environment,
            StateVersion: stateVersion,
            ObservedAtUtc: freshnessObservedAt,
            Message: isCurrent
                ? "Authenticated host state was refreshed from canonical version "
                    + stateVersion + "."
                : "Authenticated host snapshot was received at canonical version "
                    + stateVersion + ", but host freshness is "
                    + hostFreshness + ".")
        {
            IsCurrent = isCurrent,
        }.Validated();
        return new Snapshot(status, eventCursor);
    }

    private HttpRequestMessage CreateRequest(
        HttpMethod method,
        string relativePath,
        EmergencyHostSession session)
    {
        EmergencyHostSession boundSession = GetBoundSession(session);
        HttpRequestMessage request = new(method, new Uri(BaseUri, relativePath));
        request.Headers.Accept.Add(
            new MediaTypeWithQualityHeaderValue("application/json"));
        request.Headers.Authorization =
            new AuthenticationHeaderValue("AutoTrade-Session", boundSession.Token);
        request.Headers.Add("X-AutoTrade-Actor", boundSession.Actor);
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

    private static string CanonicalSessionReference(string value, string name)
    {
        bool valid = value.Length == 68
            && value.StartsWith("sid-", StringComparison.Ordinal)
            && value[4..].All(character =>
                character is >= '0' and <= '9'
                or >= 'a' and <= 'f');
        if (!valid)
        {
            throw new InvalidOperationException(
                $"{name} must be a canonical public session reference.");
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

    public static string PublicSessionReference(string token)
    {
        if (string.IsNullOrWhiteSpace(token)
            || !string.Equals(token, token.Trim(), StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Session token is invalid.",
                nameof(token));
        }

        byte[] material = Encoding.UTF8.GetBytes(
            "autotrade-ui-session-v1\0" + token);
        byte[] digest = [];
        try
        {
            digest = SHA256.HashData(material);
            return "sid-" + Convert.ToHexString(digest).ToLowerInvariant();
        }
        finally
        {
            CryptographicOperations.ZeroMemory(material);
            if (digest.Length > 0)
            {
                CryptographicOperations.ZeroMemory(digest);
            }
        }
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

    internal static Uri ValidateBaseUri(Uri value)
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
        string SessionReference,
        string AccountId,
        string Environment,
        string? HostId,
        string ExpectedStateVersion);
}

internal sealed record DesktopHostConnection(
    IEmergencyHostClient Client,
    IEmergencyHostSessionProvider? SessionProvider);

internal static class DesktopHostClientFactory
{
    public static IEmergencyHostClient Create() => CreateConnection().Client;

    public static DesktopHostConnection CreateConnection()
    {
        string? uriText = Environment.GetEnvironmentVariable("AUTOTRADE_HOST_URI");
        string? credentialTarget =
            Environment.GetEnvironmentVariable("AUTOTRADE_HOST_CREDENTIAL_TARGET");
        if (string.IsNullOrWhiteSpace(uriText)
            || string.IsNullOrWhiteSpace(credentialTarget)
            || !Uri.TryCreate(uriText.Trim(), UriKind.Absolute, out Uri? uri))
        {
            return new DesktopHostConnection(
                new DisconnectedEmergencyHostClient(
                    "Authenticated host connection is not configured. "
                    + "Set the non-secret host URI and Windows Credential Manager target after pairing."),
                null);
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
            string canonicalCredentialTarget = credentialTarget.Trim();
            WindowsCredentialManagerSessionProvider sessionProvider = new(
                canonicalCredentialTarget,
                uri);
            IEmergencyHostClient client = new AuthenticatedEmergencyHostClient(
                httpClient,
                uri,
                sessionProvider,
                new WindowsCredentialManagerPendingCommandStore(
                    canonicalCredentialTarget + ":pending-emergency-command-v1"));
            return new DesktopHostConnection(client, sessionProvider);
        }
        catch (Exception error) when (
            error is ArgumentException
            or InvalidOperationException
            or PlatformNotSupportedException)
        {
            return new DesktopHostConnection(
                new DisconnectedEmergencyHostClient(
                    "Authenticated host configuration is invalid. "
                    + "No durable emergency command can be issued until pairing/configuration is repaired."),
                null);
        }
    }
}
