"""Publish a screen-reader-friendly account statement using existing local evidence."""

from contextlib import closing
from datetime import datetime
from decimal import Decimal as D
import json
import sqlite3
from zoneinfo import ZoneInfo

from live import FOLDER
from tools.whitebit_historical_klines import _atomic_replace

NAMES = {"wait10": "Купівля після спаду на 10%", "channel": "Торгівля в діапазоні"}
BUDAPEST = ZoneInfo("Europe/Budapest")


def local(iso):
    return (
        datetime.fromisoformat(iso).astimezone(BUDAPEST).strftime("%d.%m.%Y %H:%M:%S")
    )


def number(n):
    return f"{D(n):,.2f}".replace(",", " ").replace(".", ",")


def publish():
    dbpath = FOLDER / "accounts.sqlite3"
    if not dbpath.exists():
        return
    with closing(sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True)) as db:
        data = db.execute(
            "SELECT payload FROM observations ORDER BY clock DESC LIMIT 1"
        ).fetchone()
    if not data:
        return
    original = json.loads(data[0])
    marked_at = original["recorded"]
    accounts = {
        k: {
            "nav": s["last_mark"],
            "units": s["units"],
            "realized": s["realized_usdt"],
            "cycles": s["cycles"],
        }
        for k, s in original["accounts"].items()
    }
    audit = FOLDER / "hour-20261120-morning"
    for name in ("start", "end"):
        path = audit / (name + ".json")
        if not path.is_file():
            continue
        review = json.loads(path.read_bytes())
        if datetime.fromisoformat(review["quote_time"]) > datetime.fromisoformat(
            marked_at
        ):
            accounts = review["accounts"]
            marked_at = review["quote_time"]
    lines = [
        "AutoTrade. Спотова симуляція Toncoin на WhiteBIT",
        "Час оцінки за Будапештом — " + local(marked_at),
        "Початковий капітал кожного віртуального рахунку — 1 000,00 USDT.",
        "",
    ]
    for key, s in accounts.items():
        lines.extend(
            [
                NAMES[key],
                f"Закритих угод — {s['cycles']}.",
                f"За закритими угодами — {number(s['realized'])} USDT.",
            ]
        )
        if s["nav"] is None:
            lines.append("Недостатньо обсягу в стакані для оцінки всієї позиції.")
        else:
            net = D(s["nav"]) - 1000
            opened = net - D(s["realized"])
            lines += [
                f"Вартість рахунку після витрат — {number(s['nav'])} USDT.",
                f"Від початку — {number(net)} USDT ({number(net / 10)}%).",
                f"Відкрита позиція — {number(s['units'])} GRAM. Результат відкритої позиції — {number(opened)} USDT.",
            ]
        lines.append("")
    if (audit / "end.json").is_file():
        start = json.loads((audit / "start.json").read_bytes())
        end = json.loads((audit / "end.json").read_bytes())
        lines.extend(
            [
                "Окреме вимірювання за годину"
                if end["on_time"]
                else "Окреме вимірювання із запізнілим кінцевим знімком",
                local(start["quote_time"])
                + " — "
                + local(end["quote_time"])
                + " за Будапештом.",
            ]
        )
        for key, v in end["deltas"].items():
            lines.append(
                f"{NAMES[key]} — зміна вартості {number(v['nav_usdt'])} USDT ({number(v['pct'])}%). "
                f"За закритими угодами — {number(v['realized_usdt'])} USDT."
            )
        lines.append("")
    lines.append(
        "Стакан показує доступний обсяг для продажу. Оцінка включає модельну комісію та поправку на виконання."
    )
    rendered = ("\n".join(lines) + "\n").encode("utf-8-sig")
    output = FOLDER / "Результат рахунків.txt"
    if not output.exists() or output.read_bytes() != rendered:
        _atomic_replace(output, rendered)


if __name__ == "__main__":
    publish()
