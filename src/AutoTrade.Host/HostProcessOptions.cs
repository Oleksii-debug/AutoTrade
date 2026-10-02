using System.Net;
using AutoTrade.Contracts;

namespace AutoTrade.Host;

/// <summary>
/// Non-secret identity and listener configuration for the installed host process.
/// Provider/session credentials are deliberately excluded from this configuration.
/// </summary>
public sealed record HostProcessOptions(
    Uri Origin,
    string HostId,
    string AccountId,
    string Environment,
    string Provider,
    string ProviderEnvironment)
{
    public string ListenUrl => Origin.GetLeftPart(UriPartial.Authority);

    public string CredentialTarget =>
        WindowsCredentialManagerSessionAuthenticator.CredentialTargetForOrigin(Origin);

    public static HostProcessOptions Load(IConfiguration configuration)
    {
        ArgumentNullException.ThrowIfNull(configuration);
        return Create(
            Required(configuration["AUTOTRADE_HOST_URI"], "AUTOTRADE_HOST_URI"),
            Required(configuration["AUTOTRADE_HOST_ID"], "AUTOTRADE_HOST_ID"),
            Required(configuration["AUTOTRADE_HOST_ACCOUNT_ID"], "AUTOTRADE_HOST_ACCOUNT_ID"),
            Required(configuration["AUTOTRADE_HOST_ENVIRONMENT"], "AUTOTRADE_HOST_ENVIRONMENT"),
            Required(configuration["AUTOTRADE_HOST_PROVIDER"], "AUTOTRADE_HOST_PROVIDER"),
            Required(configuration["AUTOTRADE_HOST_PROVIDER_ENVIRONMENT"], "AUTOTRADE_HOST_PROVIDER_ENVIRONMENT"));
    }

    public static HostProcessOptions Create(
        string origin,
        string hostId,
        string accountId,
        string environment,
        string provider,
        string providerEnvironment)
    {
        if (!Uri.TryCreate(Required(origin, nameof(origin)), UriKind.Absolute, out Uri? uri))
        {
            throw new InvalidOperationException("Host origin must be an absolute URI.");
        }

        if (!string.IsNullOrEmpty(uri.UserInfo)
            || !string.IsNullOrEmpty(uri.Query)
            || !string.IsNullOrEmpty(uri.Fragment)
            || uri.AbsolutePath != "/")
        {
            throw new InvalidOperationException(
                "Host origin must contain only scheme, loopback authority, and root path.");
        }

        if (!string.Equals(uri.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase)
            || !IsLoopbackHost(uri.Host))
        {
            throw new InvalidOperationException(
                "Current product host admission supports loopback HTTP only. Remote/TLS mode remains fail-closed until separately qualified.");
        }

        string canonicalHostId = Required(hostId, nameof(hostId));
        string canonicalAccountId = Required(accountId, nameof(accountId));
        string canonicalEnvironment = Required(environment, nameof(environment));
        string canonicalProvider = Required(provider, nameof(provider));
        string canonicalProviderEnvironment = Required(providerEnvironment, nameof(providerEnvironment));
        if (!string.Equals(
                canonicalProvider,
                canonicalProvider.ToUpperInvariant(),
                StringComparison.Ordinal)
            || !string.Equals(
                canonicalProviderEnvironment,
                canonicalProviderEnvironment.ToUpperInvariant(),
                StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "Host provider and provider environment must use canonical uppercase identity.");
        }
        if (!CommonScalarContracts.IsValid("Environment", canonicalEnvironment))
        {
            throw new InvalidOperationException(
                "Host environment does not satisfy the generated common Environment contract.");
        }

        return new HostProcessOptions(
            new Uri(uri.GetLeftPart(UriPartial.Authority) + "/", UriKind.Absolute),
            canonicalHostId,
            canonicalAccountId,
            canonicalEnvironment,
            canonicalProvider,
            canonicalProviderEnvironment);
    }

    public static string Route(string relativeRoute)
    {
        if (string.IsNullOrWhiteSpace(relativeRoute)
            || relativeRoute.StartsWith("/", StringComparison.Ordinal))
        {
            throw new ArgumentException("Host contract route must be canonical relative text.", nameof(relativeRoute));
        }
        return "/" + relativeRoute;
    }

    public static string OperationRoutePattern => "/api/v1/operations/{operation_id}";

    private static bool IsLoopbackHost(string host)
    {
        if (string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }
        return IPAddress.TryParse(host, out IPAddress? address)
            && IPAddress.IsLoopback(address);
    }

    private static string Required(string? value, string name)
    {
        if (string.IsNullOrWhiteSpace(value)
            || !string.Equals(value, value.Trim(), StringComparison.Ordinal))
        {
            throw new InvalidOperationException($"{name} must be canonical non-empty text.");
        }
        return value;
    }
}
