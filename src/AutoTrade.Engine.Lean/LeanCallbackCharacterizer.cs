using System.Globalization;
using System.Text.Json;
using QuantConnect.Orders;

namespace AutoTrade.Engine.Lean;

/// <summary>
/// Diagnostic-only characterization of LEAN callback arrival semantics.
/// It does not reorder events, mutate an order projection, or establish fills.
/// Canonical execution/reconciliation remains outside this probe.
/// </summary>
public sealed class LeanCallbackCharacterizer
{
    private const string StateSchemaVersion = "1.0.0";

    private static readonly JsonSerializerOptions StateJsonOptions = new()
    {
        WriteIndented = false,
    };

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

    /// <summary>
    /// Export only diagnostic callback identity state required to continue duplicate/conflict
    /// characterization after a clean process restart. This is not an AutoTrade journal,
    /// order projection, reconciliation source, or financial authority.
    /// </summary>
    public string ExportRestartState()
    {
        var callbacks = _seen
            .OrderBy(item => item.Key.OrderId)
            .ThenBy(item => item.Key.EventId)
            .Select(item => new LeanCallbackStateEntry(
                item.Key.OrderId,
                item.Key.EventId,
                item.Value.Status,
                item.Value.Symbol,
                item.Value.FillQuantity,
                item.Value.FillPrice,
                item.Value.UtcTime))
            .ToArray();

        var state = new LeanCallbackCharacterizerState(
            StateSchemaVersion,
            _lastArrivalUtc,
            callbacks);

        return JsonSerializer.Serialize(state, StateJsonOptions);
    }

    /// <summary>
    /// Restore diagnostic callback identity state created by <see cref="ExportRestartState"/>.
    /// Malformed, duplicate, ambiguous-time, or unsupported state fails closed.
    /// </summary>
    public static LeanCallbackCharacterizer RestoreRestartState(string json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            throw new ArgumentException("Restart state is required.", nameof(json));
        }

        LeanCallbackCharacterizerState? state;
        try
        {
            state = JsonSerializer.Deserialize<LeanCallbackCharacterizerState>(
                json,
                StateJsonOptions);
        }
        catch (JsonException error)
        {
            throw new InvalidDataException("LEAN callback restart state is invalid JSON.", error);
        }

        if (state is null)
        {
            throw new InvalidDataException("LEAN callback restart state is empty.");
        }

        if (!string.Equals(
                state.SchemaVersion,
                StateSchemaVersion,
                StringComparison.Ordinal))
        {
            throw new InvalidDataException("Unsupported LEAN callback restart state schema.");
        }

        if (state.Callbacks is null)
        {
            throw new InvalidDataException("LEAN callback restart state callbacks are required.");
        }

        if (state.LastArrivalUtc.HasValue &&
            state.LastArrivalUtc.Value.Kind != DateTimeKind.Utc)
        {
            throw new InvalidDataException(
                "LEAN callback restart last-arrival time must be explicitly UTC.");
        }

        if ((state.Callbacks.Count == 0) != !state.LastArrivalUtc.HasValue)
        {
            throw new InvalidDataException(
                "LEAN callback restart state arrival marker is inconsistent.");
        }

        var result = new LeanCallbackCharacterizer();
        foreach (var entry in state.Callbacks)
        {
            if (entry.UtcTime.Kind != DateTimeKind.Utc)
            {
                throw new InvalidDataException(
                    "LEAN callback restart event time must be explicitly UTC.");
            }

            var key = (entry.OrderId, entry.EventId);
            var fingerprint = new CallbackFingerprint(
                entry.Status,
                entry.Symbol ?? string.Empty,
                entry.FillQuantity,
                entry.FillPrice,
                entry.UtcTime);
            if (!result._seen.TryAdd(key, fingerprint))
            {
                throw new InvalidDataException(
                    $"Duplicate LEAN callback identity in restart state: {entry.OrderId}/{entry.EventId}.");
            }
        }

        result._lastArrivalUtc = state.LastArrivalUtc;
        return result;
    }
}

internal readonly record struct CallbackFingerprint(
    OrderStatus Status,
    string Symbol,
    decimal FillQuantity,
    decimal FillPrice,
    DateTime UtcTime);

public sealed record LeanCallbackCharacterizerState(
    string SchemaVersion,
    DateTime? LastArrivalUtc,
    IReadOnlyList<LeanCallbackStateEntry> Callbacks);

public readonly record struct LeanCallbackStateEntry(
    int OrderId,
    int EventId,
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
