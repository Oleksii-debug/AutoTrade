using AutoTrade.Contracts;

namespace AutoTrade.Desktop;

/// <summary>
/// Package-independent security contract for the primary embedded web experience.
/// The eventual WebView2 adapter must delegate origin, navigation, privileged browser
/// features and credential-forwarding decisions here instead of creating a second
/// desktop trust policy.
/// </summary>
public sealed class WebExperienceSecurityPolicy
{
    private const string RouteProbeOperationId = "00000000-0000-0000-0000-000000000000";
    private static readonly string CanonicalOperationPrefix = BuildCanonicalOperationPrefix();
    private static readonly string StatePath = "/" + HostApiRoutes.GetState;
    private static readonly string CommandPath = "/" + HostApiRoutes.SubmitCommand;
    private static readonly string EventPath = "/" + HostApiRoutes.StreamEvents;

    public WebExperienceSecurityPolicy(Uri hostOrigin)
    {
        HostOrigin = AuthenticatedEmergencyHostClient.ValidateBaseUri(hostOrigin);
    }

    /// <summary>
    /// Exact host origin already selected by the paired desktop host authority.
    /// It contains no credential material.
    /// </summary>
    public Uri HostOrigin { get; }

    /// <summary>
    /// Top-level embedded content may navigate only on the exact configured
    /// AutoTrade host origin. External schemes, hosts and ports stay outside
    /// the trusted desktop surface.
    /// </summary>
    public bool AllowsTopLevelNavigation(Uri target)
    {
        if (!IsSameHostOrigin(target)
            || !string.IsNullOrEmpty(target.Query)
            || !string.IsNullOrEmpty(target.Fragment))
        {
            return false;
        }

        return target.AbsolutePath is "/" or "/index.html";
    }

    /// <summary>
    /// Session/actor headers may be attached only to canonical versioned Host API
    /// requests on the exact paired host origin. Static assets and all other
    /// origins must remain credential-free.
    /// </summary>
    public bool AllowsSessionHeaderForwarding(
        Uri target,
        Uri topLevelDocument)
    {
        if (!AllowsTopLevelNavigation(topLevelDocument)
            || !IsSameHostOrigin(target)
            || !string.IsNullOrEmpty(target.Fragment))
        {
            return false;
        }

        string path = target.AbsolutePath;
        if (string.Equals(path, StatePath, StringComparison.Ordinal)
            || string.Equals(path, CommandPath, StringComparison.Ordinal))
        {
            return string.IsNullOrEmpty(target.Query);
        }

        if (string.Equals(path, EventPath, StringComparison.Ordinal))
        {
            return HasCanonicalEventQuery(target.Query);
        }

        if (!string.IsNullOrEmpty(target.Query)
            || !path.StartsWith(CanonicalOperationPrefix, StringComparison.Ordinal))
        {
            return false;
        }

        string operationId = path[CanonicalOperationPrefix.Length..];
        return Guid.TryParseExact(operationId, "D", out Guid parsedOperationId)
            && string.Equals(
                parsedOperationId.ToString("D"),
                operationId,
                StringComparison.Ordinal);
    }

    private static bool HasCanonicalEventQuery(string query)
    {
        if (string.IsNullOrEmpty(query))
        {
            return true;
        }

        const string prefix = "?after=";
        if (!query.StartsWith(prefix, StringComparison.Ordinal))
        {
            return false;
        }

        string value = query[prefix.Length..];
        if (value.Length == 0 || value.Contains('&'))
        {
            return false;
        }

        if (value == "0")
        {
            return true;
        }

        if (value[0] is < '1' or > '9')
        {
            return false;
        }

        foreach (char character in value)
        {
            if (character is < '0' or > '9')
            {
                return false;
            }
        }

        return true;
    }

    private static string BuildCanonicalOperationPrefix()
    {
        string route = "/" + HostApiRoutes.GetOperation(RouteProbeOperationId);
        if (!route.EndsWith(RouteProbeOperationId, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "Generated Host API operation route does not preserve its canonical parameter.");
        }

        return route[..^RouteProbeOperationId.Length];
    }

    /// <summary>
    /// Arbitrary web-message financial command authority is forbidden.
    /// Commands continue to use the canonical authenticated Host API.
    /// </summary>
    public bool AllowsWebMessageCommandAuthority => false;

    /// <summary>
    /// Release-mode embedded content must not expose browser developer tools.
    /// </summary>
    public bool AllowsDeveloperTools => false;

    /// <summary>
    /// Service workers are forbidden in the trusted financial web surface.
    /// A background same-origin worker must not gain interception authority over
    /// authenticated Host API traffic or outlive the visible trusted document.
    /// </summary>
    public bool AllowsServiceWorkers => false;

    /// <summary>
    /// Downloads are not an authority-bearing path for the embedded product UI.
    /// </summary>
    public bool AllowsDownloads => false;

    /// <summary>
    /// Popups/new windows are never silently admitted into the trusted surface.
    /// An external URI may only be handed to the OS by a separately reviewed,
    /// non-authority product flow.
    /// </summary>
    public bool AllowsNewWindow(Uri target) => false;

    private bool IsSameHostOrigin(Uri target)
    {
        if (target is null
            || !target.IsAbsoluteUri
            || !string.IsNullOrEmpty(target.UserInfo))
        {
            return false;
        }

        return Uri.Compare(
            target,
            HostOrigin,
            UriComponents.SchemeAndServer,
            UriFormat.UriEscaped,
            StringComparison.OrdinalIgnoreCase) == 0;
    }
}
