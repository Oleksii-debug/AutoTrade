from pathlib import Path

path = Path("mvp/autotrade_mvp/binance_spot.py")
text = path.read_text(encoding="utf-8")
old = "        if admitted.final_update_id < cursor.update_id:\n            return BinanceSpotDepthDecision(\"DISCARD\", None)\n"
new = "        if admitted.final_update_id <= cursor.update_id:\n            return BinanceSpotDepthDecision(\"DISCARD\", None)\n"
if text.count(old) != 1:
    raise SystemExit("expected one steady-state Binance depth stale-range boundary")
path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
