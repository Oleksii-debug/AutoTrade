"""Hourly local research task. Hour zero freezes a signal and hour one saves depth."""

from live import monitored_tick

if __name__ == "__main__":
    monitored_tick()
