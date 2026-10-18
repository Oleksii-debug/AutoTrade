namespace AutoTrade.Host;

/// <summary>
/// Process-owned splice point for the canonical journal/risk/provider execution authority.
/// The initial executable deliberately has no permissive implementation: an installed
/// process without the qualified financial authority must remain BLOCKED and must not
/// interpret HTTP request acceptance as financial completion.
/// </summary>
public interface IHostAuthorityBoundary
{
    HostAuthorityReadiness Readiness { get; }
}

public sealed record HostAuthorityReadiness(
    string Status,
    IReadOnlyList<string> ReasonCodes)
{
    public static HostAuthorityReadiness Blocked(params string[] reasonCodes) =>
        new("BLOCKED", Array.AsReadOnly(reasonCodes));
}

public sealed class UnavailableHostAuthorityBoundary : IHostAuthorityBoundary
{
    public HostAuthorityReadiness Readiness { get; } =
        HostAuthorityReadiness.Blocked(
            "canonical_financial_authority_unavailable",
            "provider_execution_issuer_unavailable");
}
