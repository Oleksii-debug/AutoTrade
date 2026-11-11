"""Interactively install a least-privilege WhiteBIT LIVE TRADE credential.

Secrets are requested through a hidden terminal prompt and are never accepted
as command-line arguments or printed.  This tool installs credential/binding
evidence only; it cannot send an order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from getpass import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvp.autotrade_mvp.whitebit_trade_setup import (  # noqa: E402
    install_whitebit_trade_binding,
    load_whitebit_trade_binding,
)
from mvp.autotrade_mvp.windows_secrets import (  # noqa: E402
    DpapiCurrentUserProtector,
    ProtectedCredentialVault,
)


def _vault(data_dir: Path) -> ProtectedCredentialVault:
    if os.name != "nt":
        raise RuntimeError("WhiteBIT TRADE setup requires Windows DPAPI")
    return ProtectedCredentialVault(
        data_dir / "credentials.json", protector=DpapiCurrentUserProtector()
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Configure protected AutoTrade WhiteBIT LIVE TRADE access"
    )
    parser.add_argument(
        "command", choices=("install", "status"), nargs="?", default="status"
    )
    parser.add_argument(
        "--data-dir",
        default=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AutoTrade-ZERO"),
    )
    parser.add_argument(
        "--ip-whitelist-confirmed",
        action="store_true",
        help="confirm that the WhiteBIT key is restricted to this machine's allowed public IP",
    )
    parser.add_argument(
        "--evidence-ref",
        default="operator-attestation:whitebit-api-key-settings",
        help="non-secret reference to the inspected WhiteBIT key settings",
    )
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve(strict=False)
    vault = _vault(data_dir)
    if args.command == "status":
        handle, boundary = load_whitebit_trade_binding(vault, data_dir=data_dir)
        print(json.dumps({
            "status": "READY_FOR_CAPABILITY_BINDING",
            "provider": handle.provider,
            "environment": handle.environment,
            "purpose": handle.purpose,
            "account_id": handle.account_id,
            "generation": handle.generation,
            "permissions": sorted(boundary.permissions),
            "ip_whitelist_enabled": boundary.ip_whitelist_enabled,
            "contains_secrets": False,
        }, sort_keys=True))
        return 0
    if not args.ip_whitelist_confirmed:
        parser.error("install requires --ip-whitelist-confirmed")
    api_key = getpass("WhiteBIT API key (hidden): ")
    api_secret = getpass("WhiteBIT API secret (hidden): ")
    handle, boundary = install_whitebit_trade_binding(
        vault,
        data_dir=data_dir,
        api_key=api_key,
        api_secret=api_secret,
        permissions=frozenset({"INFO", "TRADING"}),
        ip_whitelist_enabled=True,
        observed_at=datetime.now(timezone.utc),
        evidence_ref=args.evidence_ref,
    )
    api_key = api_secret = None
    print(json.dumps({
        "status": "INSTALLED",
        "provider": handle.provider,
        "environment": handle.environment,
        "purpose": handle.purpose,
        "account_id": handle.account_id,
        "generation": handle.generation,
        "permissions": sorted(boundary.permissions),
        "ip_whitelist_enabled": True,
        "contains_secrets": False,
        "next_step": "bind current verified WhiteBIT SPOT capability evidence",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
