"""Interactive Windows AI credential setup. Never accepts the API key as an argument."""

from __future__ import annotations

import argparse
from decimal import Decimal
from getpass import getpass
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mvp"))

from autotrade_mvp.mistral_model_adapter import MistralInferenceAdapter, MistralSettings
from autotrade_mvp.model_credentials import delete_model_key, store_model_key


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally echoes rejected argv, which could contain a key.
        self.exit(2, "Помилка аргументів. Ключ вводиться тільки інтерактивно.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(description="Налаштування ключа Mistral для AutoTrade (Windows)")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("store", "test", "delete"):
        item = commands.add_parser(command)
        item.add_argument("--profile", default="owner")
        if command == "test":
            item.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "store":
            key = getpass("Ключ Mistral (введення приховане): ")
            store_model_key(profile=args.profile, key=key)
            print("Ключ збережено у Windows Credential Manager.")
        elif args.command == "delete":
            delete_model_key(profile=args.profile)
            print("Ключ видалено.")
        else:
            settings = MistralSettings(
                profile=args.profile, model_id=args.model,
                maximum_input_bytes=1, maximum_output_tokens=1,
                input_usd_per_million=Decimal(0),
                output_usd_per_million=Decimal(0),
            )
            available = MistralInferenceAdapter(settings).test_connection()
            print("Підключення успішне; модель доступна." if available else
                  "Підключення успішне; зазначена модель недоступна для цього ключа.")
            return 0 if available else 2
    except (ValueError, RuntimeError) as error:
        # Credential and transport errors deliberately omit remote response bodies.
        print(f"Помилка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
