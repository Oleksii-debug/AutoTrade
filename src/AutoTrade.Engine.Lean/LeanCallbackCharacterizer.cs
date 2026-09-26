using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using QuantConnect.Orders;

namespace AutoTrade.Engine.Lean;

/// <summary>
/// Diagnostic-only characterization of LEAN callback arrival semantics.
/// It does not reorder events, mutate an order projection, or establish fills.
/// Canonical execution/reconciliation remains outside this probe.
/// </summary>
public sealed class LeanCallbackCharacterizer
{
    private const string StateSchemaVersion = "1.1.0";

    private static readonly JsonSerializerOptions StateJsonOptions = new()
    {
        WriteIndented = false,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
    };

    private readonly Dictionary<(int OrderId, int EventId), CallbackFingerprint> _seen = new();
    private DateTime? _arrivalHighWaterUtc;

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
        var fillPriceCurrency = orderEvent.FillPriceCurrency ?? string.Empty;
        var hasOrderFee = orderEvent.OrderFee is not null;
        var orderFeeAmount = hasOrderFee
            ? orderEvent.OrderFee.Value.Amount
            : decimal.Zero;
        var orderFeeCurrency = hasOrderFee
            ? orderEvent.OrderFee.Value.Currency ?? string.Empty
            : string.Empty;
        var fingerprint = new CallbackFingerprint(
            orderEvent.Status,
            orderEvent.Symbol?.Value ?? string.Empty,
            orderEvent.FillQuantity,
            orderEvent.FillPrice,
            fillPriceCurrency,
            hasOrderFee,
            orderFeeAmount,
            orderFeeCurrency,
            orderEvent.UtcTime);
        var duplicateIdentity = _seen.TryGetValue(identity, out var existing);
        var identityConflict = duplicateIdentity && existing != fingerprint;
        if (!duplicateIdentity)
        {
            _seen.Add(identity, fingerprint);
        }

        var timeRegressed =
            _arrivalHighWaterUtc.HasValue &&
            orderEvent.UtcTime < _arrivalHighWaterUtc.Value;
        if (!_arrivalHighWaterUtc.HasValue ||
            orderEvent.UtcTime > _arrivalHighWaterUtc.Value)
        {
            _arrivalHighWaterUtc = orderEvent.UtcTime;
        }

        return new LeanCallbackObservation(
            orderEvent.OrderId,
            orderEvent.Id,
            orderEvent.Status.ToString(),
            orderEvent.Symbol?.Value ?? string.Empty,
            orderEvent.FillQuantity.ToString(CultureInfo.InvariantCulture),
            orderEvent.FillPrice.ToString(CultureInfo.InvariantCulture),
            fillPriceCurrency,
            hasOrderFee,
            orderFeeAmount.ToString(CultureInfo.InvariantCulture),
            orderFeeCurrency,
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
                item.Value.FillPriceCurrency,
                item.Value.HasOrderFee,
                item.Value.OrderFeeAmount,
                item.Value.OrderFeeCurrency,
                item.Value.UtcTime))
            .ToArray();

        var payload = new LeanCallbackCharacterizerStatePayload(
            StateSchemaVersion,
            _arrivalHighWaterUtc,
            callbacks);
        var state = new LeanCallbackCharacterizerState(
            payload.SchemaVersion,
            payload.ArrivalHighWaterUtc,
            payload.Callbacks,
            ComputeStateHash(payload));

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
            using var document = JsonDocument.Parse(json);
            EnsureNoDuplicateJsonMembers(document.RootElement);
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

        var payload = new LeanCallbackCharacterizerStatePayload(
            state.SchemaVersion,
            state.LastArrivalUtc,
            state.Callbacks);
        var expectedStateHash = ComputeStateHash(payload);
        if (!string.Equals(state.StateHash, expectedStateHash, StringComparison.Ordinal))
        {
            throw new InvalidDataException(
                "LEAN callback restart state integrity hash mismatch.");
        }

        if (state.ArrivalHighWaterUtc.HasValue &&
            state.ArrivalHighWaterUtc.Value.Kind != DateTimeKind.Utc)
        {
            throw new InvalidDataException(
                "LEAN callback restart arrival high-water must be explicitly UTC.");
        }

        if ((state.Callbacks.Count == 0) != !state.ArrivalHighWaterUtc.HasValue)
        {
            throw new InvalidDataException(
                "LEAN callback restart state arrival high-water is inconsistent.");
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
                entry.FillPriceCurrency ?? string.Empty,
                entry.HasOrderFee,
                entry.OrderFeeAmount,
                entry.OrderFeeCurrency ?? string.Empty,
                entry.UtcTime);
            if (!result._seen.TryAdd(key, fingerprint))
            {
                throw new InvalidDataException(
                    $"Duplicate LEAN callback identity in restart state: {entry.OrderId}/{entry.EventId}.");
            }
        }

        if (state.ArrivalHighWaterUtc.HasValue &&
            state.Callbacks.Any(entry => entry.UtcTime > state.ArrivalHighWaterUtc.Value))
        {
            throw new InvalidDataException(
                "LEAN callback restart arrival high-water predates a stored callback.");
        }

        result._arrivalHighWaterUtc = state.ArrivalHighWaterUtc;
        return result;
    }

    private static void EnsureNoDuplicateJsonMembers(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in element.EnumerateObject())
            {
                if (!names.Add(property.Name))
                {
                    throw new InvalidDataException(
                        $"LEAN callback restart state contains duplicate JSON member: {property.Name}.");
                }
                EnsureNoDuplicateJsonMembers(property.Value);
            }
            return;
        }

        if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray())
            {
                EnsureNoDuplicateJsonMembers(item);
            }
        }
    }

    private static string ComputeStateHash(LeanCallbackCharacterizerStatePayload payload)
    {
        var canonical = JsonSerializer.Serialize(payload, StateJsonOptions);
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(canonical));
        return "sha256:" + Convert.ToHexString(digest).ToLowerInvariant();
    }
}

internal readonly record struct CallbackFingerprint(
    OrderStatus Status,
    string Symbol,
    decimal FillQuantity,
    decimal FillPrice,
    string FillPriceCurrency,
    bool HasOrderFee,
    decimal OrderFeeAmount,
    string OrderFeeCurrency,
    DateTime UtcTime);

internal sealed record LeanCallbackCharacterizerStatePayload(
    string SchemaVersion,
    DateTime? ArrivalHighWaterUtc,
    IReadOnlyList<LeanCallbackStateEntry> Callbacks);

public sealed record LeanCallbackCharacterizerState(
    string SchemaVersion,
    DateTime? ArrivalHighWaterUtc,
    IReadOnlyList<LeanCallbackStateEntry> Callbacks,
    string StateHash);

public readonly record struct LeanCallbackStateEntry(
    int OrderId,
    int EventId,
    OrderStatus Status,
    string Symbol,
    decimal FillQuantity,
    decimal FillPrice,
    string FillPriceCurrency,
    bool HasOrderFee,
    decimal OrderFeeAmount,
    string OrderFeeCurrency,
    DateTime UtcTime);

public readonly record struct LeanCallbackObservation(
    int OrderId,
    int EventId,
    string Status,
    string Symbol,
    string FillQuantity,
    string FillPrice,
    string FillPriceCurrency,
    bool HasOrderFee,
    string OrderFeeAmount,
    string OrderFeeCurrency,
    bool HasEconomicFill,
    bool DuplicateIdentity,
    bool IdentityConflict,
    bool TimeRegressed,
    DateTime UtcTime);
