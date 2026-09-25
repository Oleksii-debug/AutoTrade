# NVDA release qualification

Static markup checks, WPF compilation and automated accessibility-tree checks are useful gates, but they do not prove that the delivered AutoTrade build works with NVDA.

The canonical workflow list is `requirements.json`. Release qualification requires a real Windows 11 build, the delivered artifact, NVDA, keyboard-only operation and evidence for every required workflow. Evidence must identify the exact source SHA and artifact SHA-256 and record keyboard steps, the NVDA observation and a durable evidence reference for each workflow.

`tools/check_nvda_qualification.py` validates that evidence and the checked-in status. The repository currently remains explicitly unqualified: `status.json` contains `NO_REAL_NVDA_RELEASE_EVIDENCE`. Do not change it to qualified from unit tests, synthetic screen-reader output or a source-only review.
