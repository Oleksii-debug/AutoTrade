# Windows background watchdog

`tools/run_autotrade_process_watchdog.py` runs the checked-in WhiteBIT market
collectors, historical research workers, and SOL forward simulator as local
Windows processes independently of a Codex chat. The
checked-in manifest contains no credentials and every child remains
non-trading (`network_orders=false`, `trading_authority=false`).

Run it from the repository root with a persistent runtime directory that is
outside the replaceable application directory:

```powershell
python tools/run_autotrade_process_watchdog.py `
  --manifest packaging/windows/autotrade-runtime-manifest.json `
  --runtime-root D:\AutoTradeData\runtime `
  --state-dir D:\AutoTradeData\watchdog
```

The watchdog writes `watchdog-state.json`, bounded rotated diagnostics, and
separate stdout/stderr logs.  A child that exits is restarted with a bounded
crash window; repeated crashes open a circuit and leave the watchdog in
`DEGRADED` instead of spinning indefinitely.

The two managed historical research workers defer new computation cycles on
confirmed Windows battery power. They retain their results and next scheduled
run, and check for AC power every 15 seconds. The public-data collectors and
forward simulation continue while Windows is awake. After shutdown or
hibernation, they resume from their local checkpoints on restart. The worker
records gaps in live coverage. Keep the computer shut down or hibernated
during transport in a bag.

To stop safely, write any file named `watchdog.stop.json` into the watchdog
state directory or send the watchdog SIGINT/SIGTERM.  It writes a per-process
managed stop marker, waits for each process to publish its checkpoint, then
uses forced termination only after the grace period.  The next start removes
only those managed stop markers.  Runtime data, learning memory, model state,
and market evidence are never stored in the replaceable program directory.

The manifest accepts only repository-relative Python scripts.  Options named
like credentials, tokens, passwords, private keys, or secrets are rejected;
provider credentials must continue to come from the operating-system secure
store inside the child process.

For automatic startup at the current user's Windows logon, install the bundled
scheduled task once (no administrator account or stored password is required):

```powershell
.\packaging\windows\Install-AutoTradeWatchdog.ps1 `
  -RepositoryRoot $PWD `
  -RuntimeRoot D:\AutoTradeData\runtime `
  -StateDirectory D:\AutoTradeData\watchdog
```

The task uses `pythonw.exe` when it is available, prevents parallel duplicate
instances, restarts the watchdog after a process failure, and starts when a
missed logon trigger becomes available. If Windows denies Task Scheduler
registration for the current account, the installer falls back to the
non-admin per-user `HKCU\\...\\Run` startup entry; the watchdog's single-owner
lock still prevents a duplicate runtime. Remove only the startup registration
with the same command plus `-Remove`; both startup mechanisms are removed while
runtime state and learned experience are left intact.
