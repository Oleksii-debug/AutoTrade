"""Stage the reviewed production-host Python component through the canonical source TCB.

The descriptor set is module-owned.  Callers select only the destination
composition/source root; they cannot inject arbitrary source paths into the
source-controlled staging authority.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from tools.stage_windows_foundation import (
    ROOT,
    FoundationStagingError,
    _SourceControlledComponent,
    _stage_source_controlled_components,
)


_HOST_REQUIRED = (
    _SourceControlledComponent(
        "autotrade-host-01-mvp-init",
        "runtime-host-bootstrap",
        "mvp/__init__.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-02-package-init",
        "runtime-host",
        "mvp/autotrade_mvp/__init__.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-03-production-host",
        "runtime-host",
        "mvp/autotrade_mvp/production_host.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-04-host-network",
        "runtime-host",
        "mvp/autotrade_mvp/host_network.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-05-durable-host-api",
        "runtime-host",
        "mvp/autotrade_mvp/durable_host_api.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-06-host-api",
        "runtime-host",
        "mvp/autotrade_mvp/host_api.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-07-host-actions",
        "runtime-host",
        "mvp/autotrade_mvp/host_actions.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-08-persistence",
        "runtime-host",
        "mvp/autotrade_mvp/persistence.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-09-persistence-impl",
        "runtime-host",
        "mvp/autotrade_mvp/_persistence_impl.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-10-store-identity",
        "runtime-host",
        "mvp/autotrade_mvp/store_identity.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-11-security",
        "runtime-host",
        "mvp/autotrade_mvp/security.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-12-decision-trace",
        "runtime-host",
        "mvp/autotrade_mvp/decision_trace.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-13-windows-secrets",
        "runtime-host",
        "mvp/autotrade_mvp/windows_secrets.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-14-provider-domain",
        "runtime-host",
        "mvp/autotrade_mvp/provider_domain.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-15-common-scalars",
        "runtime-host",
        "mvp/autotrade_mvp/_generated_common_scalars.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-16-operator-authority",
        "runtime-host",
        "mvp/autotrade_mvp/operator_authority_commands.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-17-authority",
        "runtime-host",
        "mvp/autotrade_mvp/authority.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-18-allocation",
        "runtime-host",
        "mvp/autotrade_mvp/allocation.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-19-allocation-valuation",
        "runtime-host",
        "mvp/autotrade_mvp/allocation_valuation.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-20-durable-reservations",
        "runtime-host",
        "mvp/autotrade_mvp/durable_reservations.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-21-reservations",
        "runtime-host",
        "mvp/autotrade_mvp/reservations.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-22-reconciliation-journal",
        "runtime-host",
        "mvp/autotrade_mvp/reconciliation_journal.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-23-reconciliation",
        "runtime-host",
        "mvp/autotrade_mvp/reconciliation.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-24-securities-borrow",
        "runtime-host",
        "mvp/autotrade_mvp/securities_borrow.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-25-risk-policy-authority",
        "runtime-host",
        "mvp/autotrade_mvp/risk_policy_authority.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-26-risk",
        "runtime-host",
        "mvp/autotrade_mvp/risk.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-27-exact-decimal",
        "runtime-host",
        "mvp/autotrade_mvp/exact_decimal.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-28-dispatch",
        "runtime-host",
        "mvp/autotrade_mvp/dispatch.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-29-provider-response-limits",
        "runtime-host",
        "mvp/autotrade_mvp/provider_response_limits.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-30-sender-gate",
        "runtime-host",
        "mvp/autotrade_mvp/sender_gate.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-31-recovery",
        "runtime-host",
        "mvp/autotrade_mvp/recovery.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-32-fx-valuation",
        "runtime-host",
        "mvp/autotrade_mvp/fx_valuation.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-33-perpetuals",
        "runtime-host",
        "mvp/autotrade_mvp/perpetuals.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-34-corporate-actions",
        "runtime-host",
        "mvp/autotrade_mvp/corporate_actions.py",
    ),
    _SourceControlledComponent(
        "autotrade-host-35-instruments",
        "runtime-host",
        "mvp/autotrade_mvp/instruments.py",
    ),
)


def stage_windows_host(
    *,
    staging: Path,
    composition_path: Path,
    source_root: Path = ROOT,
    expected_source_sha: str | None = None,
) -> tuple[dict[str, str], ...]:
    """Stage the module-owned production-host dependency closure."""

    return _stage_source_controlled_components(
        staging=staging,
        composition_path=composition_path,
        source_root=source_root,
        descriptors=_HOST_REQUIRED,
        expected_source_sha=expected_source_sha,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, type=Path)
    parser.add_argument("--composition", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--expected-source-sha")
    args = parser.parse_args()
    try:
        result = stage_windows_host(
            staging=args.staging,
            composition_path=args.composition,
            source_root=args.source_root,
            expected_source_sha=args.expected_source_sha,
        )
    except FoundationStagingError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
