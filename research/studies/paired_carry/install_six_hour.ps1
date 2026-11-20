[CmdletBinding()]
param(
    [string] $Python = (Get-Command pythonw.exe -ErrorAction Stop).Source,
    [string] $TaskName = 'AutoTrade WhiteBIT Six Hour Learner 20261120'
)
$ErrorActionPreference = 'Stop'
$study = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runner = Join-Path $study 'six_hour_forward.py'
$config = Get-Content -Raw -LiteralPath (Join-Path $study 'six-hour-forward-rule.json') | ConvertFrom-Json
$first = [DateTimeOffset]$config.first_entry_utc
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$triggers = foreach ($start in @($first.AddMinutes(-25), $first.AddSeconds(22))) {
    $trigger = New-ScheduledTaskTrigger -Once -At $start.LocalDateTime -RepetitionInterval (New-TimeSpan -Hours 6)
    # UTC preserves the market clock across changes in local daylight saving time.
    $trigger.StartBoundary = $start.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ss'Z'")
    $trigger
}
$triggers += New-ScheduledTaskTrigger -AtLogOn -User $user
$action = New-ScheduledTaskAction -Execute (Resolve-Path -LiteralPath $Python).Path -Argument ('-B "' + $runner + '"') -WorkingDirectory $study
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
