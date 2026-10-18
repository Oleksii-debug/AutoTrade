"""Store/delete an approved model-provider key using hidden terminal input."""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mvp"))

from autotrade_mvp.model_credentials import delete_model_key, store_model_key
from autotrade_mvp.model_provider_registry import SUPPORTED_CREDENTIAL_PROVIDERS


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Never echo rejected argv: a user might accidentally paste a key.
        self.exit(2, "Помилка аргументів. Ключ вводиться тільки інтерактивно.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(
        description="Налаштування ключа модельного провайдера AutoTrade (Windows)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("store", "delete"):
        item = commands.add_parser(command)
        item.add_argument(
            "--provider", required=True, choices=SUPPORTED_CREDENTIAL_PROVIDERS,
        )
        item.add_argument("--profile", default="owner")
    args = parser.parse_args(argv)
    try:
        if args.command == "store":
            key = getpass("API-ключ (введення приховане): ")
            store_model_key(provider=args.provider, profile=args.profile, key=key)
            print("Ключ збережено у Windows Credential Manager.")
        else:
            delete_model_key(provider=args.provider, profile=args.profile)
            print("Ключ видалено.")
    except (ValueError, RuntimeError) as error:
        print(f"Помилка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
