# WP-45 — WPF live-region automation event

Date: 2026-09-25  
Base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

## Defect

The native WPF shell declared polite/assertive `AutomationProperties.LiveSetting` values for host-status and emergency-result text, but code-behind did not explicitly raise `AutomationEvents.LiveRegionChanged` when the text changed. A declared live setting alone is not a reproducible NVDA announcement contract.

## Increment

Both live text regions now route `TextChanged` through one handler. Once the window is loaded, that handler obtains/creates the element automation peer and raises `LiveRegionChanged`. It deliberately does not move keyboard focus; existing refresh/emergency focus-return behavior remains unchanged.

## Regression

The desktop shell contract test proves:
- exactly two live regions are wired;
- an automation peer is obtained/created;
- `LiveRegionChanged` is raised;
- the handler contains no focus move;
- pre-load changes are not announced as runtime status.

This is software-level accessibility hardening. It is not a substitute for the required real Windows + NVDA qualification evidence.
