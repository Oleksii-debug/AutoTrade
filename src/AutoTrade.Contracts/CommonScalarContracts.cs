using System.Text.RegularExpressions;

namespace AutoTrade.Contracts;

/// <summary>
/// Strict admission for the language-neutral common scalar subset.
/// Values stay textual: no float or integer coercion is performed.
/// </summary>
public static partial class CommonScalarContracts
{
    private static readonly HashSet<string> Environments =
        new(StringComparer.Ordinal) { "REPLAY", "SIMULATION", "PAPER", "LIVE" };

    /// <summary>
    /// Validates a textual common-scalar value against the named canonical scalar kind.
    /// </summary>
    /// <param name="kind">Canonical scalar kind, such as Decimal, Digest, or Environment.</param>
    /// <param name="value">Textual value to validate without numeric coercion.</param>
    /// <returns><see langword="true"/> when the value satisfies the selected canonical scalar contract.</returns>
    /// <exception cref="ArgumentOutOfRangeException">Thrown when <paramref name="kind"/> is unsupported.</exception>
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
            "CurrencyId" => CurrencyPattern().IsMatch(value),
            "UnitId" => UnitPattern().IsMatch(value),
            "Environment" => Environments.Contains(value),
            _ => throw new ArgumentOutOfRangeException(nameof(kind), kind, "Unsupported common scalar kind."),
        };
    }

    [GeneratedRegex(@"^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$", RegexOptions.CultureInvariant)]
    private static partial Regex DecimalPattern();

    [GeneratedRegex(@"^(0|[1-9][0-9]*)$", RegexOptions.CultureInvariant)]
    private static partial Regex SequencePattern();

    [GeneratedRegex(@"^sha256:[0-9a-f]{64}$", RegexOptions.CultureInvariant)]
    private static partial Regex DigestPattern();

    [GeneratedRegex(@"^[A-Za-z0-9._:-]{1,32}$", RegexOptions.CultureInvariant)]
    private static partial Regex CurrencyPattern();

    [GeneratedRegex(@"^[A-Za-z0-9._:/-]{1,64}$", RegexOptions.CultureInvariant)]
    private static partial Regex UnitPattern();
}
