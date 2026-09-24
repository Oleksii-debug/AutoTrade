namespace AutoTrade.Desktop;

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
    Task<EmergencyCommandResult> BlockNewExposureAsync(CancellationToken cancellationToken);
}

/// <summary>
/// Safe default used until the authenticated local host client is connected.
/// It never fabricates a durable safety action.
/// </summary>
public sealed class DisconnectedEmergencyHostClient : IEmergencyHostClient
{
    public Task<EmergencyCommandResult> BlockNewExposureAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return Task.FromResult(
            new EmergencyCommandResult(
                false,
                "Host is not connected. No durable block of new exposure has been confirmed."));
    }
}
