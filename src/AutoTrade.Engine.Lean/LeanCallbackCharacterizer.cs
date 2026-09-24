using System.Globalization;
using QuantConnect.Orders;

namespace AutoTrade.Engine.Lean;

/// <summary>
/// Diagnostic-only characterization of LEAN callback arrival semantics.
/// It does not reorder events, mutate an order projection, or establish fills.
/// Canonical execution/reconciliation remains outside this probe.
/// </summary>
public sealed class LeanCallbackCharacterizer
{
    private readonly HashSet<(int OrderId, int EventId)> _seen = new();
    private DateTime? _lastArrivalUtc;

    public LeanCallbackObservation Observe(OrderEvent orderEvent)
    {
        ArgumentNullException.ThrowIfNull(orderEvent);

        if (orderEvent.UtcTime.Kind != DateTimeKind.Utc)
        {
            throw new ArgumentException(
                "LEAN callback time must be explicitly UTC.",
                nameof(orderEvent));
        }

        var identity = (orderEvent.OrderId, orderEvent.Id);
        var duplicateIdentity = !_seen.Add(identity);
        var timeRegressed =
            _lastArrivalUtc.HasValue && orderEvent.UtcTime < _lastArrivalUtc.Value;
        _lastArrivalUtc = orderEvent.UtcTime;

        return new LeanCallbackObservation(
            orderEvent.OrderId,
            orderEvent.Id,
            orderEvent.Status.ToString(),
            orderEvent.Symbol?.Value ?? string.Empty,
            orderEvent.FillQuantity.ToString(CultureInfo.InvariantCulture),
            orderEvent.FillPrice.ToString(CultureInfo.InvariantCulture),
            orderEvent.FillQuantity != decimal.Zero,
            duplicateIdentity,
            timeRegressed,
            orderEvent.UtcTime);
    }
}

public readonly record struct LeanCallbackObservation(
    int OrderId,
    int EventId,
    string Status,
    string Symbol,
    string FillQuantity,
    string FillPrice,
    bool HasEconomicFill,
    bool DuplicateIdentity,
    bool TimeRegressed,
    DateTime UtcTime);
