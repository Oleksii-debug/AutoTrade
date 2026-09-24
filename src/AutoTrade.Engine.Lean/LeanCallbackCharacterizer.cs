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
    private readonly Dictionary<(int OrderId, int EventId), CallbackFingerprint> _seen = new();
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
        var fingerprint = new CallbackFingerprint(
            orderEvent.Status,
            orderEvent.Symbol?.Value ?? string.Empty,
            orderEvent.FillQuantity,
            orderEvent.FillPrice,
            orderEvent.UtcTime);
        var duplicateIdentity = _seen.TryGetValue(identity, out var existing);
        var identityConflict = duplicateIdentity && existing != fingerprint;
        if (!duplicateIdentity)
        {
            _seen.Add(identity, fingerprint);
        }

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
            identityConflict,
            timeRegressed,
            orderEvent.UtcTime);
    }
}

internal readonly record struct CallbackFingerprint(
    OrderStatus Status,
    string Symbol,
    decimal FillQuantity,
    decimal FillPrice,
    DateTime UtcTime);

public readonly record struct LeanCallbackObservation(
    int OrderId,
    int EventId,
    string Status,
    string Symbol,
    string FillQuantity,
    string FillPrice,
    bool HasEconomicFill,
    bool DuplicateIdentity,
    bool IdentityConflict,
    bool TimeRegressed,
    DateTime UtcTime);
