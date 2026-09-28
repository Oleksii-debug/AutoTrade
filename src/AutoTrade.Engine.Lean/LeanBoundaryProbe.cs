using System.Globalization;
using System.Text.RegularExpressions;
using QuantConnect;
using QuantConnect.Orders;

namespace AutoTrade.Engine.Lean;

/// <summary>
/// Non-sending characterization boundary for the exact approved LEAN revision.
/// It proves only type/decimal/order-object compatibility and never grants
/// brokerage or financial authority.
/// </summary>
public static class LeanBoundaryProbe
{
    public const string ExpectedLeanRevision =
        "985ef30ad3ac774218c5ac516b4cb0aa2655730f";

    private const string CanonicalDecimalPattern =
        @"^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$";

    public static LeanBoundaryResult CreateOrderProjection(
        string ticker,
        string market,
        string quantityText,
        string priceText,
        DateTime utcTime)
    {
        EnsureToken(ticker, nameof(ticker));
        EnsureToken(market, nameof(market));

        if (utcTime.Kind != DateTimeKind.Utc)
        {
            throw new ArgumentException("Order time must be explicitly UTC.", nameof(utcTime));
        }

        var quantity = ParseCanonicalDecimal(quantityText, nameof(quantityText));
        var price = ParseCanonicalDecimal(priceText, nameof(priceText));

        if (quantity == decimal.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(quantityText), "Quantity cannot be zero.");
        }

        if (price <= decimal.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(priceText), "Price must be positive.");
        }

        // The adoption probe must not depend on LEAN data/map-file configuration.
        // We still exercise the exact SecurityIdentifier/Symbol/Order types, but
        // deliberately disable equity ticker mapping for this non-sending boundary.
        var sid = SecurityIdentifier.GenerateEquity(ticker, market, mapSymbol: false);
        var symbol = new Symbol(sid, ticker);
        var order = new MarketOrder(
            symbol,
            quantity,
            utcTime,
            price,
            "AUTOTRADE-WP02-NON-SENDING-PROBE");

        if (order.Quantity != quantity || order.Price != price)
        {
            throw new InvalidOperationException(
                "LEAN changed an authoritative decimal value at the adoption boundary.");
        }

        return new LeanBoundaryResult(
            order.Symbol.Value,
            market,
            order.Quantity.ToString(CultureInfo.InvariantCulture),
            order.Price.ToString(CultureInfo.InvariantCulture));
    }

    private static decimal ParseCanonicalDecimal(string text, string parameterName)
    {
        if (string.IsNullOrEmpty(text) ||
            !Regex.IsMatch(text, CanonicalDecimalPattern, RegexOptions.CultureInvariant))
        {
            throw new ArgumentException(
                "Value must satisfy the canonical AutoTrade Decimal contract.",
                parameterName);
        }

        if (!decimal.TryParse(
                text,
                NumberStyles.AllowLeadingSign | NumberStyles.AllowDecimalPoint,
                CultureInfo.InvariantCulture,
                out var value))
        {
            throw new ArgumentOutOfRangeException(
                parameterName,
                "Canonical decimal is outside System.Decimal range.");
        }

        return value;
    }

    private static void EnsureToken(string value, string parameterName)
    {
        if (string.IsNullOrWhiteSpace(value) ||
            !string.Equals(value, value.Trim(), StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "Identifier must be non-empty and free of surrounding whitespace.",
                parameterName);
        }
    }
}

public readonly record struct LeanBoundaryResult(
    string Symbol,
    string Market,
    string Quantity,
    string Price);
