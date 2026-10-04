# WP-32 exact valuation boundary — 2026-09-25

## Scope

This evidence note records the convergence work for issue #639 on exact base
`main@7a05c926c0d0989ba74e0bf8f1ed050a35b71b71` after repeated semantic zero-overlap reconvergence from the original `4de3a4c5fdf1642a53676009f94d5b1fd61133e7` development base.

The change extends the existing WP-32 proposal allocator; it does not create a
second portfolio, risk, accounting, provider, execution or reconciliation
authority.

## Implemented invariants

- allocation evidence now has an explicit immutable `VALUATION` kind;
- the reconciled capital evidence declares one portfolio `base_currency`;
- every selected candidate is normalized into that base currency before gross,
  net, cost, capital and stress aggregation;
- single-currency linear instruments retain exact Decimal identity conversion;
- cross-currency linear instruments require a fresh exact canonical FX quote and
  bind its rate, source identity and evidence SHA-256;
- linear futures/perpetuals use the canonical contract multiplier through the
  existing linear-notional boundary;
- explicit execution, financing, funding, borrow and FX cost components must sum
  exactly to the candidate cost rate and each component carries an evidence
  identity;
- inverse contracts and options fail closed rather than falling through to a
  linear `quantity * price` approximation;
- valuation evidence participates in the decision digest and admission-time
  freshness/re-resolution checks;
- binary floating-point remains rejected at the financial boundary.

## Focused regression surface

The added/updated tests cover:

- unchanged single-currency cash-equity valuation;
- fresh EUR→USD conversion plus missing/stale FX rejection;
- exact linear-future contract multiplier;
- inverse and option fail-closed behavior;
- explicit cost-component completeness and float rejection;
- valuation evidence identity changing the decision digest without changing the
  underlying allocation arithmetic;
- valuation expiry invalidating admission;
- existing authority-allocation binding fixtures using the same valuation-bound
  proposal.

## Deliberate limits

This is not terminal WP-32 qualification.  The nonlinear valuation adapter for
inverse futures/perpetuals and options is not implemented in this lineage; those
families are rejected for increased allocation until their canonical payoff
evidence is wired.  Financing/funding/borrow components are bound explicitly but
their provider/account qualification remains separate.  No economic edge is
claimed, no provider credential is used, no live order is sent, and no real-money
authority is granted.

Merge only after exact-head baseline and full Verify AutoTrade succeed on
Windows and Ubuntu, with the reconvergence integrity guard green.
