namespace AutoTrade.Contracts;

/// <summary>
/// AUTO-GENERATED from contracts/openapi/host-api.yaml. DO NOT EDIT.
/// Run python tools/generate_host_api_routes.py to regenerate.
/// Route values are relative to the validated host origin.
/// </summary>
public static class HostApiRoutes
{
    public const string GetState = "api/v1/state";

    public const string SubmitCommand = "api/v1/commands";

    public static string GetOperation(string operationId)
    {
        if (string.IsNullOrWhiteSpace(operationId) || !string.Equals(operationId, operationId.Trim(), StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Host API route parameter operation_id is required and must be canonical.",
                nameof(operationId));
        }

        return "api/v1/operations/" + Uri.EscapeDataString(operationId);
    }

    public const string StreamEvents = "api/v1/events";

    public const string GetHealth = "api/v1/health";

}
