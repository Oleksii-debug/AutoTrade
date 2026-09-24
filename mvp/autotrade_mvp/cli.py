"""Command-line demo for the safe simulated AutoTrade MVP."""

from dataclasses import asdict
import argparse
import json

from .pipeline import run_vertical_slice


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the network-free AutoTrade vertical-slice demo")
    parser.add_argument("--state-dir", default="mvp-state")
    parser.add_argument("--prices", default="100,101,102,103")
    args = parser.parse_args()
    result = run_vertical_slice(args.prices.split(","), args.state_dir)
    print(json.dumps({key: str(value) for key, value in asdict(result).items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
