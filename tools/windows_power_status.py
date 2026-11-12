"""Read the Windows AC-line flag without starting a subprocess."""
import ctypes
import os


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ac_line_status", ctypes.c_ubyte),
        ("battery_flag", ctypes.c_ubyte),
        ("battery_life_percent", ctypes.c_ubyte),
        ("system_status_flag", ctypes.c_ubyte),
        ("battery_life_seconds", ctypes.c_uint32),
        ("battery_full_life_seconds", ctypes.c_uint32),
    ]


def running_on_battery() -> bool:
    """Recognize a confirmed battery supply. Keep the prior policy on other hosts."""
    if os.name != "nt":
        return False
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False
    return status.ac_line_status == 0 and not status.battery_flag & 128
