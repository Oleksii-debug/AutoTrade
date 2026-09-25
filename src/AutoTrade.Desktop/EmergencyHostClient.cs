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
/// Result of requesting an emergency host command. Acceptance is deliberately
/// distinct from any later financial/provider outcome.
/// </summary>
public sealed record EmergencyCommandResult(bool Accepted, string Message);

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
                false,
                "Host is not connected. No durable block of new exposure has been confirmed."));
    }
}
