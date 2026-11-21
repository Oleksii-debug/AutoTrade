"""Check a 48-hour holding interval using the same confirmed long-loser/short-winner pair."""
from decimal import Decimal as D
from hashlib import sha256
import json
from data import CODE,ROOT,DAY,HOUR,WEEK,WhiteBitPublicClient
from momentum_pair import ranking_price
from relative_funding import episode
from reversal_confirmation import MARKETS,confirm,load_era


def run_block(panel,lows,rules,first,last):
    wallets=dict.fromkeys(("confirmed","fixed","opposite"),D(1000))
    peaks=dict(wallets)
    drawdown=dict.fromkeys(wallets,D(0))
    weeks=[]
    client=WhiteBitPublicClient()
    for monday in range(first,last,WEEK):
        initial=dict(wallets)
        periods=[]
        for t in (monday,monday+2*DAY,monday+4*DAY):
            cutoff=t-DAY
            score={m:ranking_price(m,cutoff,panel[m][1],client)/ranking_price(m,cutoff-WEEK,panel[m][1],client)
                   for m in MARKETS}
            order=sorted(MARKETS,key=lambda m:(score[m],m))
            loser,winner=order[0],order[-1]
            entry=confirm(panel,t,loser,winner,window=12*HOUR)
            record=dict(t=t,cutoff=cutoff,confirmed_entry=entry,loser=loser,winner=winner,accounts={})
            for arm,start,buy,sell in (("fixed",t,loser,winner),("confirmed",entry,loser,winner),("opposite",entry,winner,loser)):
                if start is None:
                    continue
                if any(panel[m][2][k][2]<=0 for m in (buy,sell) for k in (start,t+2*DAY)):
                    raise ValueError("Missing volume at fill")
                path,metrics=episode(panel,lows,rules,start,buy,sell,wallets[arm],end_clock=t+2*DAY)
                if not metrics["margin_ok"]:
                    raise ValueError("Collateral breach")
                for v in path:
                    peaks[arm]=max(v,peaks[arm])
                    drawdown[arm]=max(drawdown[arm],1-v/peaks[arm])
                record["accounts"][arm]=dict(net=path[-1]-wallets[arm],**metrics)
                wallets[arm]=path[-1]
            periods.append(record)
        weeks.append(dict(monday=monday,periods=periods,returns={a:wallets[a]/initial[a]-1 for a in wallets}))
    geometric=(wallets["confirmed"]/1000)**(D(1)/len(weeks))-1
    return dict(first=first,ending=wallets,drawdown=drawdown,weekly_geometric=geometric,
                target_weeks=sum(w["returns"]["confirmed"]>=D(".02") for w in weeks),
                eligible=geometric>=D(".02") and wallets["confirmed"]>max(wallets["fixed"],wallets["opposite"]),weeks=weeks)


if __name__=="__main__":
    blocks=[]
    for era in ("additional","prior","recent"):
        cuts,panel,rules,lows=load_era(era)
        benchmark=json.loads((ROOT/f"reversal-confirm-{era}.json").read_bytes())["blocks"]
        for (a,b),weekly in zip(zip(cuts,cuts[1:]),benchmark,strict=True):
            block=run_block(panel,lows,rules,a,b)
            if (a,b)!=(weekly["first"],weekly["last"]):
                raise ValueError("Weekly benchmark period mismatch")
            block["weekly_confirmation_benchmark"]=weekly.get("ending",{}).get("confirmed")
            baseline=block["weekly_confirmation_benchmark"]
            block["eligible"]=bool(block["eligible"] and baseline is not None and block["ending"]["confirmed"]>D(baseline))
            blocks.append(block)
            print(a,block["ending"],block["target_weeks"],flush=True)
        del panel,lows
    rule=sha256((CODE/"reversal-two-day-rule.json").read_bytes()).hexdigest()
    (ROOT/"reversal-two-day-results.json").write_text(json.dumps(dict(rule=rule,blocks=blocks),default=str,separators=(",",":")))
