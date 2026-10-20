"""Run one bounded, restart-safe, research-only Groq advisory wave."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "mvp"))

from autotrade_mvp.groq_advisory_research import (
    DurableGroqAdvisoryWave,
    GroqAdvisoryError,
    advisory_result_json,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AutoTrade four-role Groq ZERO research advisory"
    )
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--wave-id", required=True)
    args = parser.parse_args(argv)
    try:
        # Canonicalize ordinary relative CLI paths before the durable filesystem
        # boundary validates Windows namespace components such as ``..``.
        input_path = Path(args.input_json).expanduser().resolve(strict=True)
        with input_path.open("r", encoding="utf-8") as stream:
            research_input = json.load(stream)
        state_directory = Path(args.state_dir).expanduser().resolve(strict=False)
        runner = DurableGroqAdvisoryWave(state_directory)
        result = runner.run(wave_id=args.wave_id, research_input=research_input)
        print(advisory_result_json(result))
        return 0 if result.status == "COMPLETED" else 2
    except (OSError, ValueError, json.JSONDecodeError, GroqAdvisoryError) as error:
        print(f"Groq advisory failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
