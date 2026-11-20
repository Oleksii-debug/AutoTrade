"""Hourly local research task. Hour zero freezes a signal and hour one saves depth."""

from live import monitored_tick
from pnl_report import publish

if __name__ == "__main__":
    monitored_tick()
    publish()
