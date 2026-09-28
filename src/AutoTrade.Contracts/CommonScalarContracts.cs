using System.Text.RegularExpressions;

namespace AutoTrade.Contracts;

/// <summary>
/// AUTO-GENERATED strict admission for the language-neutral common scalar subset.
/// Run python tools/generate_common_scalar_bindings.py to regenerate.
/// </summary>
public static partial class CommonScalarContracts
{
    private static readonly HashSet<string> EnvironmentValues =
        new(StringComparer.Ordinal) { "REPLAY", "SIMULATION", "PAPER", "LIVE" };

    /// <summary>Validates a textual common scalar without numeric coercion.</summary>
    public static bool IsValid(string kind, string? value)
    {
        if (value is null)
        {
            return false;
        }

        return kind switch
        {
            "Decimal" => DecimalPattern().IsMatch(value),
            "Sequence" => SequencePattern().IsMatch(value),
            "Digest" => DigestPattern().IsMatch(value),
            "CurrencyId" => value.Length >= 1 && value.Length <= 32 && CurrencyIdPattern().IsMatch(value),
            "UnitId" => value.Length >= 1 && value.Length <= 64 && UnitIdPattern().IsMatch(value),
            "Environment" => EnvironmentValues.Contains(value),
            _ => throw new ArgumentOutOfRangeException(nameof(kind), kind, "Unsupported common scalar kind."),
        };
    }

    [GeneratedRegex(@"^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex DecimalPattern();

    [GeneratedRegex(@"^(0|[1-9][0-9]*)(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex SequencePattern();

    [GeneratedRegex(@"^sha256:[0-9a-f]{64}(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex DigestPattern();

    [GeneratedRegex(@"^[A-Za-z0-9._:-]+(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex CurrencyIdPattern();

    [GeneratedRegex(@"^[A-Za-z0-9._:/-]+(?![\s\S])", RegexOptions.CultureInvariant)]
    private static partial Regex UnitIdPattern();

}
