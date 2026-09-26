using AutoTrade.Engine.Lean;
using QuantConnect;
using QuantConnect.Orders;
using System.Xml.Linq;

static void Require(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

static void ExpectFailure<TException>(Action action, string name)
    where TException : Exception
{
    try
    {
        action();
    }
    catch (TException)
    {
        return;
    }

    throw new InvalidOperationException($"Expected {typeof(TException).Name}: {name}");
}

Require(
    LeanBoundaryProbe.ExpectedLeanRevision ==
        "985ef30ad3ac774218c5ac516b4cb0aa2655730f",
    "The probe is not bound to the approved LEAN revision.");

var instant = new DateTime(2026, 9, 24, 20, 0, 0, DateTimeKind.Utc);

if (args.Length > 0)
{
    Require(args.Length == 2, "Restart probe requires exactly one state-file path.");
    var restartStatePath = args[1];
    var restartSymbol = new Symbol(
        SecurityIdentifier.GenerateEquity("SPY", Market.USA, mapSymbol: false),
        "SPY");

    if (args[0] == "--write-restart-state")
    {
        var checkpointCallbacks = new LeanCallbackCharacterizer();
        checkpointCallbacks.Observe(new OrderEvent
        {
            OrderId = 84,
            Id = 1,
            Symbol = restartSymbol,
            UtcTime = instant,
            Status = OrderStatus.Submitted,
            FillQuantity = decimal.Zero,
            FillPrice = decimal.Zero
        });
        checkpointCallbacks.Observe(new OrderEvent
        {
            OrderId = 84,
            Id = 2,
            Symbol = restartSymbol,
            UtcTime = instant.AddMilliseconds(1),
            Status = OrderStatus.PartiallyFilled,
            FillQuantity = 0.25m,
            FillPrice = 451.125m
        });
        File.WriteAllText(
            restartStatePath,
            checkpointCallbacks.ExportRestartState());
        Console.WriteLine("WP02_LEAN_RESTART_CHECKPOINT_WRITTEN");
        return;
    }

    if (args[0] == "--resume-restart-state")
    {
        var resumedCallbacks = LeanCallbackCharacterizer.RestoreRestartState(
            File.ReadAllText(restartStatePath));

        var repeated = resumedCallbacks.Observe(new OrderEvent
        {
            OrderId = 84,
            Id = 2,
            Symbol = restartSymbol,
            UtcTime = instant.AddMilliseconds(1),
            Status = OrderStatus.PartiallyFilled,
            FillQuantity = 0.25m,
            FillPrice = 451.125m
        });
        Require(
            repeated.DuplicateIdentity && !repeated.IdentityConflict,
            "Restart lost an identical callback identity.");

        var conflicting = resumedCallbacks.Observe(new OrderEvent
        {
            OrderId = 84,
            Id = 2,
            Symbol = restartSymbol,
            UtcTime = instant.AddMilliseconds(1),
            Status = OrderStatus.PartiallyFilled,
            FillQuantity = 0.25m,
            FillPrice = 451.500m
        });
        Require(
            conflicting.DuplicateIdentity && conflicting.IdentityConflict,
            "Restart hid conflicting economics for the same callback identity.");

        var regressedAfterRestart = resumedCallbacks.Observe(new OrderEvent
        {
            OrderId = 84,
            Id = 3,
            Symbol = restartSymbol,
            UtcTime = instant,
            Status = OrderStatus.Filled,
            FillQuantity = 0.75m,
            FillPrice = 451.125m
        });
        Require(
            regressedAfterRestart.TimeRegressed,
            "Restart lost the previous arrival-time boundary.");
        Require(
            regressedAfterRestart.HasEconomicFill,
            "Restart characterization lost the economic fill.");

        Console.WriteLine("WP02_LEAN_RESTART_RESUME_PASS");
        return;
    }

    throw new InvalidOperationException($"Unknown restart probe mode: {args[0]}");
}
var buy = LeanBoundaryProbe.CreateOrderProjection(
    "SPY",
    "usa",
    "123.45",
    "451.125",
    instant);

Require(buy.Symbol == "SPY", "LEAN changed the equity symbol identity.");
Require(buy.Market == "usa", "Market identity changed across the boundary.");
Require(buy.Quantity == "123.45", "Decimal quantity did not round-trip exactly.");
Require(buy.Price == "451.125", "Decimal price did not round-trip exactly.");

var sell = LeanBoundaryProbe.CreateOrderProjection(
    "SPY",
    "usa",
    "-2.5",
    "451.125",
    instant);
Require(sell.Quantity == "-2.5", "Signed quantity did not round-trip exactly.");

ExpectFailure<ArgumentException>(
    () => LeanBoundaryProbe.CreateOrderProjection("SPY", "usa", "1e2", "10", instant),
    "exponent form must not enter the financial boundary");
ExpectFailure<ArgumentException>(
    () => LeanBoundaryProbe.CreateOrderProjection("SPY", "usa", "1.0", "10", instant),
    "non-canonical trailing zero must be rejected");
ExpectFailure<ArgumentOutOfRangeException>(
    () => LeanBoundaryProbe.CreateOrderProjection("SPY", "usa", "0", "10", instant),
    "zero quantity must be rejected");
ExpectFailure<ArgumentOutOfRangeException>(
    () => LeanBoundaryProbe.CreateOrderProjection("SPY", "usa", "1", "-1", instant),
    "non-positive price must be rejected");
ExpectFailure<ArgumentException>(
    () => LeanBoundaryProbe.CreateOrderProjection(
        "SPY",
        "usa",
        "1",
        "10",
        DateTime.SpecifyKind(instant, DateTimeKind.Unspecified)),
    "ambiguous wall-clock time must be rejected");

var symbol = new Symbol(
    SecurityIdentifier.GenerateEquity("SPY", Market.USA, mapSymbol: false),
    "SPY");
var callbacks = new LeanCallbackCharacterizer();

var submitted = callbacks.Observe(new OrderEvent
{
    OrderId = 42,
    Id = 1,
    Symbol = symbol,
    UtcTime = instant,
    Status = OrderStatus.Submitted,
    FillQuantity = decimal.Zero,
    FillPrice = decimal.Zero
});
Require(submitted.Status == "Submitted", "Acknowledgement status was not preserved.");
Require(!submitted.HasEconomicFill, "Acknowledgement must not be characterized as a fill.");
Require(!submitted.DuplicateIdentity, "First callback identity was marked duplicate.");
Require(!submitted.IdentityConflict, "First callback identity cannot conflict.");
Require(!submitted.TimeRegressed, "First callback cannot regress time.");

var partial = callbacks.Observe(new OrderEvent
{
    OrderId = 42,
    Id = 2,
    Symbol = symbol,
    UtcTime = instant.AddMilliseconds(1),
    Status = OrderStatus.PartiallyFilled,
    FillQuantity = 0.25m,
    FillPrice = 451.125m
});
Require(partial.Status == "PartiallyFilled", "Partial-fill status was not preserved.");
Require(partial.HasEconomicFill, "Non-zero fill quantity was lost.");
Require(partial.FillQuantity == "0.25", "Callback fill quantity changed.");
Require(partial.FillPrice == "451.125", "Callback fill price changed.");
Require(!partial.DuplicateIdentity, "New callback identity was marked duplicate.");

var duplicate = callbacks.Observe(new OrderEvent
{
    OrderId = 42,
    Id = 2,
    Symbol = symbol,
    UtcTime = instant.AddMilliseconds(1),
    Status = OrderStatus.PartiallyFilled,
    FillQuantity = 0.25m,
    FillPrice = 451.125m
});
Require(duplicate.DuplicateIdentity, "Duplicate callback identity was not surfaced.");
Require(!duplicate.IdentityConflict, "Identical duplicate callback was marked conflicting.");

var conflictingDuplicate = callbacks.Observe(new OrderEvent
{
    OrderId = 42,
    Id = 2,
    Symbol = symbol,
    UtcTime = instant.AddMilliseconds(1),
    Status = OrderStatus.PartiallyFilled,
    FillQuantity = 0.25m,
    FillPrice = 451.500m
});
Require(
    conflictingDuplicate.DuplicateIdentity,
    "Conflicting repeated callback identity was not surfaced as duplicate.");
Require(
    conflictingDuplicate.IdentityConflict,
    "Same callback identity with different economics was not surfaced as conflict.");

var regressed = callbacks.Observe(new OrderEvent
{
    OrderId = 42,
    Id = 3,
    Symbol = symbol,
    UtcTime = instant,
    Status = OrderStatus.Filled,
    FillQuantity = 0.75m,
    FillPrice = 451.125m
});
Require(regressed.TimeRegressed, "Arrival-time regression was silently hidden.");
Require(regressed.HasEconomicFill, "Final non-zero fill was not characterized.");

var restartIntegrity = new LeanCallbackCharacterizer();
restartIntegrity.Observe(new OrderEvent
{
    OrderId = 73,
    Id = 1,
    Symbol = symbol,
    UtcTime = instant,
    Status = OrderStatus.PartiallyFilled,
    FillQuantity = 0.5m,
    FillPrice = 451.125m
});
var restartCheckpoint = restartIntegrity.ExportRestartState();
var verifiedRestart = LeanCallbackCharacterizer.RestoreRestartState(restartCheckpoint);
var verifiedDuplicate = verifiedRestart.Observe(new OrderEvent
{
    OrderId = 73,
    Id = 1,
    Symbol = symbol,
    UtcTime = instant,
    Status = OrderStatus.PartiallyFilled,
    FillQuantity = 0.5m,
    FillPrice = 451.125m
});
Require(
    verifiedDuplicate.DuplicateIdentity && !verifiedDuplicate.IdentityConflict,
    "Integrity-bound restart checkpoint did not preserve callback identity.");

var tamperedRestartCheckpoint = restartCheckpoint.Replace(
    "\"FillPrice\":451.125",
    "\"FillPrice\":451.5",
    StringComparison.Ordinal);
Require(
    tamperedRestartCheckpoint != restartCheckpoint,
    "Restart integrity regression did not mutate the serialized callback economics.");
ExpectFailure<InvalidDataException>(
    () => LeanCallbackCharacterizer.RestoreRestartState(tamperedRestartCheckpoint),
    "tampered LEAN callback restart economics must fail integrity verification");

Require(
    restartCheckpoint.StartsWith("{", StringComparison.Ordinal),
    "Restart checkpoint must serialize as a JSON object.");
var checkpointWithUnknownTopLevel = restartCheckpoint.Insert(
    1,
    "\"UnexpectedTopLevel\":\"forbidden\",");
ExpectFailure<InvalidDataException>(
    () => LeanCallbackCharacterizer.RestoreRestartState(checkpointWithUnknownTopLevel),
    "unknown top-level restart state fields must fail closed");

var checkpointWithUnknownCallbackField = restartCheckpoint.Replace(
    "\"OrderId\":73",
    "\"UnexpectedCallbackField\":true,\"OrderId\":73",
    StringComparison.Ordinal);
Require(
    checkpointWithUnknownCallbackField != restartCheckpoint,
    "Restart schema regression did not mutate the callback object.");
ExpectFailure<InvalidDataException>(
    () => LeanCallbackCharacterizer.RestoreRestartState(checkpointWithUnknownCallbackField),
    "unknown callback restart state fields must fail closed");

var engineIdentity = LeanEngineAssemblyProbe.GetIdentity();
Require(
    engineIdentity.TypeName == "QuantConnect.Lean.Engine.Engine",
    "The real LEAN Engine type was not resolved.");
Require(
    engineIdentity.AssemblyName == "QuantConnect.Lean.Engine",
    "The real LEAN Engine assembly was not compiled into the project graph.");

var integrationProjectPath = Path.Combine(
    Directory.GetCurrentDirectory(),
    "src",
    "AutoTrade.Engine.Lean",
    "AutoTrade.Engine.Lean.csproj");
var integrationProject = XDocument.Load(integrationProjectPath);
var directProjectReferences = integrationProject
    .Descendants("ProjectReference")
    .Select(element => element.Attribute("Include")?.Value ?? string.Empty)
    .ToHashSet(StringComparer.Ordinal);

Require(
    directProjectReferences.SetEquals(new[]
    {
        "$(LeanRoot)/Common/QuantConnect.csproj",
        "$(LeanRoot)/Engine/QuantConnect.Lean.Engine.csproj"
    }),
    "LEAN boundary direct project references drifted from the approved isolation set.");
Require(
    !integrationProject.Descendants("PackageReference").Any(),
    "LEAN boundary must not introduce direct package dependencies.");
Require(
    directProjectReferences.All(reference =>
        !reference.Contains("Brokerages", StringComparison.OrdinalIgnoreCase)),
    "LEAN boundary must not directly reference a brokerage sender project.");

Console.WriteLine("WP02_LEAN_ADOPTION_PROBE_PASS");
