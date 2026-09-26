// AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.
// Run python tools/generate_common_scalar_bindings.py to regenerate.
export declare const CONTRACT_VERSION: "4.0.0";

export type CommonScalarKind =
    "Decimal"
  | "Sequence"
  | "Digest"
  | "CurrencyId"
  | "UnitId"
  | "Environment";

export declare function isValidCommonScalar(
  kind: CommonScalarKind,
  value: unknown
): boolean;
