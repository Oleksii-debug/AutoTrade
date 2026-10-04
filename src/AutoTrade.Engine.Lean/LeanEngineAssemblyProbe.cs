namespace AutoTrade.Engine.Lean;

/// <summary>
/// Compile-time proof that AutoTrade resolves the approved LEAN Engine project,
/// not only its common DTO assembly. This does not instantiate or run LEAN.
/// </summary>
public static class LeanEngineAssemblyProbe
{
    public static LeanEngineAssemblyIdentity GetIdentity()
    {
        var type = typeof(QuantConnect.Lean.Engine.Engine);
        var assembly = type.Assembly.GetName();
        return new LeanEngineAssemblyIdentity(
            type.FullName ?? string.Empty,
            assembly.Name ?? string.Empty);
    }
}

public readonly record struct LeanEngineAssemblyIdentity(
    string TypeName,
    string AssemblyName);
