"""Attribute the frozen weekly forecasts to selection, exposure and execution costs."""
from datetime import date
from hashlib import sha256
import json
from statistics import mean

from data import CODE, ROOT
from swing import D, authenticated_bars
from weekly_agent import WEEK, account


def audit(transfer):
    profile = "weekly-transfer" if transfer else "weekly-agent"
    study = json.loads((ROOT / (profile + "-results.json")).read_bytes())
    for key, filename in [("protocol_sha256", "weekly-agent-rule.json"),
                          *([("transfer_sha256", "weekly-transfer-rule.json")] if transfer else [])]:
        if study[key] != sha256((CODE / filename).read_bytes()).hexdigest():
            raise ValueError("Stored forecasts use different rules")
    policy = json.loads((CODE / (profile + "-rule.json")).read_bytes())
    markets = policy["universe"]
    panel = {}
    for market in markets:
        path = ROOT / "daily" / "spot" / market / "1d" / "1640995200-1794787200.manifest.json"
        bars, digest, meta = authenticated_bars(path)
        if meta["market"] != market or study["data_sha256"][market] != digest:
            raise ValueError("Forecast and evaluation price archives differ")
        panel[market] = {b["opened_at"].date(): {k: D(b[k]) for k in ("open", "close")} for b in bars}
    rules = json.loads((ROOT / ("transfer-market-rules.json" if transfer else "market-rules.json")).read_bytes())
    original = {date.fromisoformat(d["asof"]): d for d in study["decisions"]}
    if len(original) != 104 or len(original) != len(study["decisions"]):
        raise ValueError("Expected exactly 104 distinct forecasts")
    cuts = list(map(date.fromisoformat, policy["cuts"]))
    blocks = []
    for index, (first, last) in enumerate(zip(cuts, cuts[1:])):
        rows, decisions = [], {}
        for i in range(52):
            day = first + i * WEEK
            prediction = original[day]
            scores = prediction["scores"]
            if set(scores) != set(markets) or date.fromisoformat(prediction["last_outcome"]) > day-WEEK:
                raise ValueError("Invalid forecast universe or training cutoff")
            ranked = sorted(sorted(markets), key=scores.get, reverse=True)
            top, bottom = ranked[0], ranked[-1]
            selected = top if prediction["expected_net"] > 0 else None
            decisions[day] = {"target": selected}
            realized = {m: panel[m][day+WEEK]["open"] / panel[m][day]["open"] - 1 for m in markets}
            rows.append(dict(day=day, top=top, selected=selected, scores=scores, realized=realized,
                             best=realized[top] == max(realized.values()),
                             top_minus_basket=realized[top]-mean(realized.values()),
                             top_minus_bottom=realized[top]-realized[bottom],
                             bias=D(str(scores[top]))-realized[top]))
        original_result = account(first, last, panel, rules, original, "model")
        if original_result["net"] != D(study["periods"][index]["model"]["net"]):
            raise ValueError("Original account replay diverged")
        accounts = {name: account(first, last, panel, rules, decisions, name) for name in ("model","clock","passive")}
        candidate = accounts["model"]
        passes = (candidate["geometric_weekly"] >= D(".02") and candidate["without_best"] > 0 and
                  candidate["net"] > max(D(0), accounts["clock"]["net"], accounts["passive"]["net"]))
        blocks.append(dict(first=first, last=last, accounts=accounts, original_net=original_result["net"],
                           top_correct=sum(row["best"] for row in rows), rows=rows, eligible=passes,
                           averages={k: mean(row[k] for row in rows) for k in ("top_minus_basket","top_minus_bottom","bias")}))
    return blocks


if __name__ == "__main__":
    result = {name: audit(transfer) for name,transfer in (("original",False),("transfer",True))}
    for name,blocks in result.items():
        for block in blocks:
            model = block["accounts"]["model"]
            print(name,block["first"],"active",model["active"],"net",round(model["net"],2),
                  "matched",round(block["accounts"]["clock"]["net"],2),"passive",round(block["accounts"]["passive"]["net"],2),
                  "weekly",round(model["geometric_weekly"],5),"top correct",block["top_correct"],"eligible",block["eligible"])
    result["rule_sha256"] = sha256((CODE / "weekly-decision-audit-rule.json").read_bytes()).hexdigest()
    (ROOT / "weekly-decision-audit.json").write_text(json.dumps(result, default=str, separators=(",",":")))
