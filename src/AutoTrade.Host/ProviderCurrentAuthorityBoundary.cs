namespace AutoTrade.Host;

internal sealed record ProviderConnectionRequest(
    string ProviderId,
    string AccountId,
    string RuntimeEnvironment,
    string ProviderEnvironment,
    string TopicId,
    string CredentialHandleId);

internal sealed record ProviderCurrentRouteAuthority(
    string ProviderId,
    string AccountId,
    string RuntimeEnvironment,
    string ProviderEnvironment,
    string Endpoint,
    string TopicId,
    string SubscriptionIdentity,
    string CapabilityId,
    string QualificationId,
    string QualificationBuildId,
    string NetworkPolicyIdentity,
    string TransportIdentity);

internal sealed record ProviderAuthenticatedReadRequest(
    string ProviderId,
    string AccountId,
    string RuntimeEnvironment,
    string ProviderEnvironment,
    string Endpoint,
    string Surface,
    string PermissionScope,
    string InstrumentVersion,
    string QueryDigest,
    string CredentialHandleId,
    IReadOnlyDictionary<string, string> Query);

internal sealed record ProviderCurrentAuthenticatedReadAuthority(
    string ProviderId,
    string AccountId,
    string EntityId,
    string RuntimeEnvironment,
    string ProviderEnvironment,
    string Endpoint,
    string Surface,
    string PermissionScope,
    string DataEntitlement,
    string InstrumentVersion,
    string QueryDigest,
    string EndpointRuleIdentity,
    string CredentialHandleId,
    string CapabilityId,
    string QualificationId,
    string QualificationBuildId,
    string AdapterBuildIdentity,
    string NetworkPolicyIdentity,
    string TransportIdentity,
    IReadOnlyDictionary<string, string> Query);

/// <summary>
/// Internal splice point between the durable current C+Q authority and the
/// credential-bearing provider process. It is intentionally not implemented
/// by configuration, callbacks, or caller booleans. Until the canonical durable
/// authority bridge exists in AutoTrade.Host, the only product implementation is
/// fail-closed.
/// </summary>
internal interface IProviderCurrentAuthorityBoundary
{
    ProviderCurrentRouteAuthority RequireCurrent(ProviderConnectionRequest request);

    void RequireStillCurrent(ProviderCurrentRouteAuthority authority);
}

internal sealed class UnavailableProviderCurrentAuthorityBoundary : IProviderCurrentAuthorityBoundary
{
    public ProviderCurrentRouteAuthority RequireCurrent(ProviderConnectionRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        throw new ProviderIssuerAuthorityException(
            "canonical current provider C+Q authority is unavailable in AutoTrade.Host");
    }

    public void RequireStillCurrent(ProviderCurrentRouteAuthority authority)
    {
        ArgumentNullException.ThrowIfNull(authority);
        throw new ProviderIssuerAuthorityException(
            "canonical current provider C+Q authority is unavailable in AutoTrade.Host");
    }
}

/// <summary>
/// Typed current-authority splice for authenticated HTTP reads. The request
/// carries only the requested read scope. Entity identity, data entitlement,
/// endpoint-rule identity, accepted C/Q/build, network policy and transport
/// identity must come back from the canonical durable authority.
/// </summary>
internal interface IProviderAuthenticatedReadAuthorityBoundary
{
    ProviderCurrentAuthenticatedReadAuthority RequireCurrent(
        ProviderAuthenticatedReadRequest request);

    void RequireStillCurrent(ProviderCurrentAuthenticatedReadAuthority authority);
}

internal sealed class UnavailableProviderAuthenticatedReadAuthorityBoundary
    : IProviderAuthenticatedReadAuthorityBoundary
{
    public ProviderCurrentAuthenticatedReadAuthority RequireCurrent(
        ProviderAuthenticatedReadRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        throw new ProviderIssuerAuthorityException(
            "canonical current authenticated-read C+Q authority is unavailable in AutoTrade.Host");
    }

    public void RequireStillCurrent(
        ProviderCurrentAuthenticatedReadAuthority authority)
    {
        ArgumentNullException.ThrowIfNull(authority);
        throw new ProviderIssuerAuthorityException(
            "canonical current authenticated-read C+Q authority is unavailable in AutoTrade.Host");
    }
}
