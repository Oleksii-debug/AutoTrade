namespace AutoTrade.Desktop;

/// <summary>
/// Read-only host identity and connection evidence shown by the native safety surface.
/// A disconnected result never implies that provider orders were cancelled or exposure is flat.
/// </summary>
public sealed record EmergencyHostStatus(
    bool Connected,
    string HostId,
    string AccountId,
    string Environment,
    string StateVersion,
    DateTimeOffset ObservedAtUtc,
    string Message)
{
    public EmergencyHostStatus Validated()
    {
        if (string.IsNullOrWhiteSpace(Message))
        {
            throw new InvalidOperationException("Host status message is required.");
        }

        if (ObservedAtUtc == default || ObservedAtUtc.Offset != TimeSpan.Zero)
        {
            throw new InvalidOperationException(
                "Host status evidence time must be a non-default UTC instant.");
        }

        if (ObservedAtUtc > DateTimeOffset.UtcNow)
        {
            throw new InvalidOperationException(
                "Host status evidence time cannot be in the future.");
        }

        if (Connected)
        {
            ValidateConnectedIdentity(HostId, nameof(HostId));
            ValidateConnectedIdentity(AccountId, nameof(AccountId));
            ValidateConnectedIdentity(Environment, nameof(Environment));
            ValidateConnectedIdentity(StateVersion, nameof(StateVersion));
            ValidateEnvironment(Environment);
            ValidateStateVersion(StateVersion);
        }

        return this;
    }

    public EmergencyHostStatus ValidateSuccessorOf(EmergencyHostStatus previous)
    {
        if (previous is null)
        {
            throw new ArgumentNullException(nameof(previous));
        }

        EmergencyHostStatus current = Validated();
        EmergencyHostStatus prior = previous.Validated();
        if (!current.Connected || !prior.Connected)
        {
            throw new InvalidOperationException(
                "Host evidence succession requires two connected observations.");
        }

        if (!string.Equals(current.HostId, prior.HostId, StringComparison.Ordinal)
            || !string.Equals(current.AccountId, prior.AccountId, StringComparison.Ordinal)
            || !string.Equals(current.Environment, prior.Environment, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "Connected host authority identity changed without an explicit transition.");
        }

        if (CompareCanonicalSequence(current.StateVersion, prior.StateVersion) < 0)
        {
            throw new InvalidOperationException(
                "Connected host state version cannot regress.");
        }

        if (current.ObservedAtUtc < prior.ObservedAtUtc)
        {
            throw new InvalidOperationException(
                "Connected host evidence time cannot regress.");
        }

        return current;
    }

    private static int CompareCanonicalSequence(string left, string right)
    {
        ValidateStateVersion(left);
        ValidateStateVersion(right);
        if (left.Length != right.Length)
        {
            return left.Length.CompareTo(right.Length);
        }

        return string.Compare(left, right, StringComparison.Ordinal);
    }

    private static void ValidateConnectedIdentity(string value, string name)
    {
        if (string.IsNullOrWhiteSpace(value)
            || !string.Equals(value, value.Trim(), StringComparison.Ordinal)
            || string.Equals(value, "Unavailable", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                $"Connected host status requires canonical {name} evidence.");
        }
    }

    private static void ValidateEnvironment(string value)
    {
        if (value is not ("REPLAY" or "SIMULATION" or "PAPER" or "LIVE"))
        {
            throw new InvalidOperationException(
                "Connected host status environment must be REPLAY, SIMULATION, PAPER, or LIVE.");
        }
    }

    private static void ValidateStateVersion(string value)
    {
        bool canonical = value == "0";
        if (!canonical && value.Length > 0 && value[0] is >= '1' and <= '9')
        {
            canonical = true;
            foreach (char character in value)
            {
                if (character is < '0' or > '9')
                {
                    canonical = false;
                    break;
                }
            }
        }

        if (!canonical)
        {
            throw new InvalidOperationException(
                "Connected host state version must be a canonical non-negative integer sequence string.");
        }
    }

    public static EmergencyHostStatus Disconnected(string message) =>
        new(
            false,
            "Unavailable",
            "Unavailable",
            "Unavailable",
            "Unavailable",
            DateTimeOffset.UtcNow,
            string.IsNullOrWhiteSpace(message)
                ? throw new ArgumentException("Disconnected status message is required.", nameof(message))
                : message.Trim());
}

/// <summary>
/// Outstanding provider/financial work after an emergency host command.
/// UNKNOWN is materially different from NONE and must remain visible.
/// </summary>
public enum InFlightActionState
{
    Unknown,
    None,
    Present,
}

/// <summary>
/// Result of requesting an emergency host command. Request acceptance, durable
/// host-side blocking and outstanding provider work are separate facts.
/// </summary>
public sealed record EmergencyCommandResult
{
    public bool Accepted { get; }
    public bool DurableBlockConfirmed { get; }
    public InFlightActionState InFlightActions { get; }
    public string OperationId { get; }
    public string Message { get; }

    public EmergencyCommandResult(
        bool accepted,
        bool durableBlockConfirmed,
        InFlightActionState inFlightActions,
        string operationId,
        string message)
    {
        if (durableBlockConfirmed && !accepted)
        {
            throw new ArgumentException(
                "A durable block cannot be confirmed for a rejected request.",
                nameof(durableBlockConfirmed));
        }

        string canonicalOperationId = "Unavailable";
        if (accepted)
        {
            if (string.IsNullOrWhiteSpace(operationId)
                || !Guid.TryParse(operationId.Trim(), out Guid parsedOperationId))
            {
                throw new ArgumentException(
                    "An accepted emergency request requires a canonical UUID operation identity.",
                    nameof(operationId));
            }

            canonicalOperationId = parsedOperationId.ToString("D");
        }

        if (string.IsNullOrWhiteSpace(message))
        {
            throw new ArgumentException("A command result message is required.", nameof(message));
        }

        Accepted = accepted;
        DurableBlockConfirmed = durableBlockConfirmed;
        InFlightActions = inFlightActions;
        OperationId = canonicalOperationId;
        Message = message.Trim();
    }
}

/// <summary>
/// Narrow desktop dependency for the native safety surface. The production
/// implementation must call the authenticated host API; it must not contain
/// trading logic or provider credentials.
/// </summary>
public enum EmergencyOperationState
{
    Accepted,
    Running,
    Succeeded,
    Failed,
    Cancelled,
    Unknown,
}

/// <summary>
/// Read-only recovery state for the exact emergency operation already accepted by
/// the canonical host. Reading this state must never create or resubmit a command.
/// </summary>
public sealed record EmergencyOperationStatus
{
    public string OperationId { get; }
    public EmergencyOperationState State { get; }
    public bool DurableBlockConfirmed { get; }
    public InFlightActionState InFlightActions { get; }
    public string Message { get; }
    public string RemainingUncertainty { get; }

    public EmergencyOperationStatus(
        string operationId,
        EmergencyOperationState state,
        bool durableBlockConfirmed,
        InFlightActionState inFlightActions,
        string message,
        string remainingUncertainty)
    {
        if (string.IsNullOrWhiteSpace(operationId)
            || !Guid.TryParse(operationId.Trim(), out Guid parsedOperationId))
        {
            throw new ArgumentException(
                "Emergency operation recovery requires a canonical UUID operation identity.",
                nameof(operationId));
        }

        string canonicalOperationId = parsedOperationId.ToString("D");
        if (!string.Equals(operationId, canonicalOperationId, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Emergency operation recovery identity must use canonical UUID serialization.",
                nameof(operationId));
        }

        if (durableBlockConfirmed && state != EmergencyOperationState.Succeeded)
        {
            throw new ArgumentException(
                "A durable block may be confirmed only by a succeeded emergency operation.",
                nameof(durableBlockConfirmed));
        }

        if (state == EmergencyOperationState.Succeeded && !durableBlockConfirmed)
        {
            throw new ArgumentException(
                "A succeeded emergency operation must carry durable block confirmation.",
                nameof(durableBlockConfirmed));
        }

        if (string.IsNullOrWhiteSpace(message))
        {
            throw new ArgumentException(
                "Emergency operation recovery requires a status message.",
                nameof(message));
        }

        if (string.IsNullOrWhiteSpace(remainingUncertainty))
        {
            throw new ArgumentException(
                "Emergency operation recovery must state remaining uncertainty explicitly.",
                nameof(remainingUncertainty));
        }

        OperationId = canonicalOperationId;
        State = state;
        DurableBlockConfirmed = durableBlockConfirmed;
        InFlightActions = inFlightActions;
        Message = message.Trim();
        RemainingUncertainty = remainingUncertainty.Trim();
    }
}

/// <summary>
/// Narrow desktop dependency for the native safety surface. The production
/// implementation must call the authenticated host API; it must not contain
/// trading logic or provider credentials.
/// </summary>
public interface IEmergencyHostClient
{
    Task<EmergencyHostStatus> GetStatusAsync(CancellationToken cancellationToken);

    Task<EmergencyCommandResult> BlockNewExposureAsync(CancellationToken cancellationToken);

    Task<EmergencyOperationStatus> GetOperationAsync(
        string operationId,
        CancellationToken cancellationToken);
}

/// <summary>
/// Safe default used until the authenticated local host client is connected.
/// It never fabricates host state or a durable safety action.
/// </summary>
public sealed class DisconnectedEmergencyHostClient : IEmergencyHostClient
{
    private readonly string _reason;

    public DisconnectedEmergencyHostClient(
        string reason = "Host is not connected. Displayed financial state cannot be refreshed.")
    {
        if (string.IsNullOrWhiteSpace(reason))
        {
            throw new ArgumentException("Disconnected host reason is required.", nameof(reason));
        }

        _reason = reason.Trim();
    }

    public Task<EmergencyHostStatus> GetStatusAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(EmergencyHostStatus.Disconnected(_reason));
    }

    public Task<EmergencyCommandResult> BlockNewExposureAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(
            new EmergencyCommandResult(
                accepted: false,
                durableBlockConfirmed: false,
                inFlightActions: InFlightActionState.Unknown,
                operationId: "Unavailable",
                message: _reason + " No durable block of new exposure has been confirmed."));
    }

    public Task<EmergencyOperationStatus> GetOperationAsync(
        string operationId,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (string.IsNullOrWhiteSpace(operationId)
            || !Guid.TryParse(operationId.Trim(), out Guid parsedOperationId))
        {
            throw new ArgumentException(
                "Emergency operation recovery requires a canonical UUID operation identity.",
                nameof(operationId));
        }

        string canonicalOperationId = parsedOperationId.ToString("D");
        if (!string.Equals(operationId, canonicalOperationId, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Emergency operation recovery identity must use canonical UUID serialization.",
                nameof(operationId));
        }

        return Task.FromResult(
            new EmergencyOperationStatus(
                operationId: canonicalOperationId,
                state: EmergencyOperationState.Unknown,
                durableBlockConfirmed: false,
                inFlightActions: InFlightActionState.Unknown,
                message: "Host is not connected. The accepted operation cannot be recovered from this client.",
                remainingUncertainty: "The durable block outcome and outstanding in-flight actions remain unknown."));
    }
}
