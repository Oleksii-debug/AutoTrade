# WP-45 — WPF live-region automation event

Date: 2026-09-25  
Base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

## Defect

The native WPF shell declared polite/assertive `AutomationProperties.LiveSetting` values for host-status and emergency-result text, but code-behind did not explicitly raise `AutomationEvents.LiveRegionChanged` when the text changed. A declared live setting alone is not a reproducible NVDA announcement contract.

## Increment

`TextBlock` does not expose WPF `TextChanged`, so dynamic status mutation now goes through one explicit `SetLiveRegionText` helper instead of invalid XAML event wiring. The helper assigns the text and, once the window is loaded, obtains/creates the element automation peer and raises `LiveRegionChanged`. It deliberately does not move keyboard focus; existing refresh/emergency focus-return behavior remains unchanged.

## Regression

The desktop shell contract test proves:
- the XAML keeps the two `LiveSetting` regions without unsupported `TextChanged` wiring;
- every host-status/emergency-result mutation goes through the explicit helper;
- an automation peer is obtained/created;
- `LiveRegionChanged` is raised;
- the handler contains no focus move;
- pre-load changes are not announced as runtime status.

This is software-level accessibility hardening. It is not a substitute for the required real Windows + NVDA qualification evidence.
