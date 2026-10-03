namespace AutoTrade.Desktop;

/// <summary>
/// Package-independent security contract for the primary embedded web experience.
/// The eventual WebView2 adapter must delegate origin, navigation and credential
/// forwarding decisions here instead of creating a second desktop trust policy.
/// </summary>
public sealed class WebExperienceSecurityPolicy
{
    private const string CanonicalApiRoot = "/api/v1";

    public WebExperienceSecurityPolicy(Uri hostOrigin)
    {
        HostOrigin = AuthenticatedEmergencyHostClient.ValidateBaseUri(hostOrigin);
    }

    /// <summary>
    /// The exact host origin already selected by the paired desktop host authority.
    /// It contains no credential material.
    /// </summary>
    public Uri HostOrigin { get; }

    /// <summary>
    /// True only for top-level content on the exact configured AutoTrade host
    /// origin. External schemes, hosts and ports stay outside the embedded surface.
    /// </summary>
    public bool AllowsTopLevelNavigation(Uri target) =>
        IsSameHostOrigin(target);

    /// <summary>
    /// Session/actor headers may be attached only to canonical versioned Host API
    /// requests on the exact paired host origin. Static assets and all other
    /// origins must remain bearer-free.
    /// </summary>
    public bool AllowsSessionHeaderForwarding(Uri target)
    {
        if (!IsSameHostOrigin(target))
        {
            return false;
        }

        string path = target.AbsolutePath;
        return string.Equals(path, CanonicalApiRoot, StringComparison.Ordinal)
            || path.StartsWith(CanonicalApiRoot + "/", StringComparison.Ordinal);
    }

    /// <summary>
    /// Arbitrary web-message command authority is forbidden. Financial commands
    /// continue to use the canonical authenticated Host API.
    /// </summary>
    public bool AllowsWebMessageCommandAuthority => false;

    /// <summary>
    /// Release-mode embedded content must not expose browser developer tools.
    /// </summary>
    public bool AllowsDeveloperTools => false;

    /// <summary>
    /// Downloads are not an authority-bearing path for the embedded product UI.
    /// </summary>
    public bool AllowsDownloads => false;

    /// <summary>
    /// Popups/new windows are never silently admitted into the trusted surface.
    /// The future adapter may explicitly hand an external URI to the OS only after
    /// a separate product decision; it must not treat that URI as trusted content.
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
