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
    public static EmergencyHostStatus Disconnected(string message) =>
        new(
            false,
            "Unavailable",
            "Unavailable",
            "Unavailable",
            "Unavailable",
            DateTimeOffset.UtcNow,
            message);
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

        if (accepted && string.IsNullOrWhiteSpace(operationId))
        {
            throw new ArgumentException(
                "An accepted emergency request requires an operation identity.",
                nameof(operationId));
        }

        if (string.IsNullOrWhiteSpace(message))
        {
            throw new ArgumentException("A command result message is required.", nameof(message));
        }

        Accepted = accepted;
        DurableBlockConfirmed = durableBlockConfirmed;
        InFlightActions = inFlightActions;
        OperationId = string.IsNullOrWhiteSpace(operationId)
            ? "Unavailable"
            : operationId.Trim();
        Message = message.Trim();
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
}

/// <summary>
/// Safe default used until the authenticated local host client is connected.
/// It never fabricates host state or a durable safety action.
/// </summary>
public sealed class DisconnectedEmergencyHostClient : IEmergencyHostClient
{
    public Task<EmergencyHostStatus> GetStatusAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(
            EmergencyHostStatus.Disconnected(
                "Host is not connected. Displayed financial state cannot be refreshed."));
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
                message: "Host is not connected. No durable block of new exposure has been confirmed."));
    }
}
