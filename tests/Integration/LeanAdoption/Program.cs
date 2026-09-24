using AutoTrade.Engine.Lean;
using QuantConnect;
using QuantConnect.Orders;

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

var symbol = Symbol.Create("SPY", SecurityType.Equity, Market.USA);
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

Console.WriteLine("WP02_LEAN_ADOPTION_PROBE_PASS");
