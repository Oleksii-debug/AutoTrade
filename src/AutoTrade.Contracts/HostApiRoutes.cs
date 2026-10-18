namespace AutoTrade.Contracts;

/// <summary>
/// AUTO-GENERATED from contracts/openapi/host-api.yaml. DO NOT EDIT.
/// Run python tools/generate_host_api_routes.py to regenerate.
/// Route values are relative to the validated host origin.
/// </summary>
public static class HostApiRoutes
{
    /// <summary>
    /// Relative route for OpenAPI operation getState.
    /// </summary>
    public const string GetState = "api/v1/state";

    /// <summary>
    /// Relative route for OpenAPI operation submitCommand.
    /// </summary>
    public const string SubmitCommand = "api/v1/commands";

    /// <summary>
    /// Resolve the relative route for OpenAPI operation getOperation.
    /// </summary>
    /// <param name="operationId">Canonical value for operation_id.</param>
    /// <returns>The relative route with encoded path parameters.</returns>
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

    /// <summary>
    /// Relative route for OpenAPI operation streamEvents.
    /// </summary>
    public const string StreamEvents = "api/v1/events";

    /// <summary>
    /// Relative route for OpenAPI operation getHealth.
    /// </summary>
    public const string GetHealth = "api/v1/health";

    /// <summary>
    /// Relative route for OpenAPI operation getHistoricalStatus.
    /// </summary>
    public const string GetHistoricalStatus = "api/v1/historical/status";

    /// <summary>
    /// Relative route for OpenAPI operation getHistoricalResults.
    /// </summary>
    public const string GetHistoricalResults = "api/v1/historical/results";

    /// <summary>
    /// Relative route for OpenAPI operation getHistoricalTrades.
    /// </summary>
    public const string GetHistoricalTrades = "api/v1/historical/trades";

    /// <summary>
    /// Relative route for OpenAPI operation startHistorical.
    /// </summary>
    public const string StartHistorical = "api/v1/historical/start";

    /// <summary>
    /// Relative route for OpenAPI operation pauseHistorical.
    /// </summary>
    public const string PauseHistorical = "api/v1/historical/pause";

    /// <summary>
    /// Relative route for OpenAPI operation stopHistorical.
    /// </summary>
    public const string StopHistorical = "api/v1/historical/stop";

    /// <summary>
    /// Relative route for OpenAPI operation resumeHistorical.
    /// </summary>
    public const string ResumeHistorical = "api/v1/historical/resume";

    /// <summary>
    /// Relative route for OpenAPI operation exportHistorical.
    /// </summary>
    public const string ExportHistorical = "api/v1/historical/export";

    /// <summary>
    /// Relative route for OpenAPI operation downloadLocalDiagnostics.
    /// </summary>
    public const string DownloadLocalDiagnostics = "api/v1/local-diagnostics";

    /// <summary>
    /// Relative route for OpenAPI operation downloadTechnicalDiagnostics.
    /// </summary>
    public const string DownloadTechnicalDiagnostics = "api/v1/technical-diagnostics";

    /// <summary>
    /// Relative route for OpenAPI operation downloadAgentReport.
    /// </summary>
    public const string DownloadAgentReport = "api/v1/agent-report";

}
