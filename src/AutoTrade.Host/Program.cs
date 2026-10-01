using AutoTrade.Contracts;
using AutoTrade.Host;

WebApplicationBuilder builder = WebApplication.CreateBuilder(args);
HostProcessOptions options = HostProcessOptions.Load(builder.Configuration);
builder.WebHost.UseUrls(options.ListenUrl);
builder.Services.AddSingleton(options);
builder.Services.AddSingleton<WindowsCredentialManagerSessionAuthenticator>();
builder.Services.AddSingleton<IHostAuthorityBoundary, UnavailableHostAuthorityBoundary>();

WebApplication app = builder.Build();
app.Use(async (context, next) =>
{
    context.Response.Headers.CacheControl = "no-store";
    await next(context);
});

app.MapGet(HostProcessOptions.Route(HostApiRoutes.GetHealth),
    (IHostAuthorityBoundary authority) =>
    {
        HostAuthorityReadiness readiness = authority.Readiness;
        return Results.Json(
            new
            {
                component = "AUTOTRADE_HOST",
                as_of = HostContractTime.FormatUtcInstant(DateTimeOffset.UtcNow),
                status = readiness.Status,
                affected_scope = new[] { "HOST_API", "FINANCIAL_EXECUTION" },
                reason_codes = readiness.ReasonCodes,
                next_action = "Bind and qualify the canonical journal/risk/provider authority inside AutoTrade.Host before enabling financial commands.",
            });
    });

app.MapGet(HostProcessOptions.Route(HostApiRoutes.GetState), ProtectedBlocked);
app.MapPost(HostProcessOptions.Route(HostApiRoutes.SubmitCommand), ProtectedBlocked);
app.MapGet(HostProcessOptions.OperationRoutePattern, ProtectedBlocked);
app.MapGet(HostProcessOptions.Route(HostApiRoutes.StreamEvents), ProtectedBlocked);

await app.RunAsync();

static IResult ProtectedBlocked(
    HttpRequest request,
    WindowsCredentialManagerSessionAuthenticator authenticator,
    IHostAuthorityBoundary authority)
{
    if (!authenticator.TryAuthenticate(request, out AuthenticatedHostSession? session)
        || session is null)
    {
        return Results.Json(
            new { error = "AUTHENTICATION_REQUIRED" },
            statusCode: StatusCodes.Status401Unauthorized);
    }

    HostAuthorityReadiness readiness = authority.Readiness;
    return Results.Json(
        new
        {
            error = "HOST_AUTHORITY_UNAVAILABLE",
            status = readiness.Status,
            reason_codes = readiness.ReasonCodes,
        },
        statusCode: StatusCodes.Status503ServiceUnavailable);
}
