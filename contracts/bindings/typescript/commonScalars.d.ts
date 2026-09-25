export declare const CONTRACT_VERSION: "2.0.0";

export type CommonScalarKind =
  | "Decimal"
  | "Sequence"
  | "Digest"
  | "CurrencyId"
  | "UnitId"
  | "Environment";

export declare function isValidCommonScalar(
  kind: CommonScalarKind,
  value: unknown
): boolean;
