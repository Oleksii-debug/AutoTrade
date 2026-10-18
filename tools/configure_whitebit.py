"""Interactive WhiteBIT READ credential setup; secrets are never argv values."""
from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mvp"))
sys.path.insert(0, str(_ROOT / "research"))

from autotrade_mvp.whitebit_credentials import (  # noqa: E402
    WHITEBIT_PRIMARY_PROFILE,
    delete_whitebit_read_credentials,
    resolve_whitebit_read_credentials,
    store_whitebit_read_credentials,
)
from autotrade_research.integrations.whitebit_private_readonly import (  # noqa: E402
    WhitebitPrivateReader,
)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse can echo a rejected argv value. Keep all secret-like input out
        # of diagnostics by returning a fixed message instead.
        self.exit(
            2,
            "Помилка аргументів. Ключ і секрет вводяться тільки інтерактивно.\n",
        )


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(
        description="WhiteBIT account READ-only для AutoTrade (Windows)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("store", "test", "delete"):
        item = commands.add_parser(command)
        item.add_argument("--profile", default=WHITEBIT_PRIMARY_PROFILE)
    args = parser.parse_args(argv)
    try:
        if args.command == "store":
            api_key = getpass("WhiteBIT API key (введення приховане): ")
            api_secret = getpass("WhiteBIT API secret (введення приховане): ")
            store_whitebit_read_credentials(
                profile=args.profile, api_key=api_key, api_secret=api_secret
            )
            print(
                "WhiteBIT READ-only облікові дані збережено у Windows "
                "Credential Manager."
            )
        elif args.command == "delete":
            delete_whitebit_read_credentials(profile=args.profile)
            print("WhiteBIT READ-only облікові дані видалено.")
        else:
            credentials = resolve_whitebit_read_credentials(profile=args.profile)
            with WhitebitPrivateReader(
                credentials.api_key,
                credentials.api_secret,
                enable_network=True,
            ) as reader:
                # Connection qualification is deliberately limited to these two
                # account reads. Bodies and balance values are never printed.
                reader.main_balance()
                reader.trading_balance()
            print("WhiteBIT READ-only підключення успішне; обидва баланси доступні.")
    except (ValueError, RuntimeError):
        # Credential and remote errors are intentionally replaced, not echoed.
        print("Помилка WhiteBIT READ-only підключення або сховища.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
