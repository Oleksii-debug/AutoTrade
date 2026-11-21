[CmdletBinding()]
param([string] $Python = (Get-Command pythonw.exe -ErrorAction Stop).Source)
$ErrorActionPreference = 'Stop'
$task = 'AutoTrade WhiteBIT Weekly Research Review 20261121'
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$script = Join-Path $PSScriptRoot 'market_ranking_forward.py'
$action = New-ScheduledTaskAction -Execute (Resolve-Path -LiteralPath $Python).Path -WorkingDirectory $PSScriptRoot -Argument ('-B "' + $script + '"')
$triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday,Monday -At '06:20')
    (New-ScheduledTaskTrigger -AtLogOn -User $owner)
)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 2
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Principal $principal -Settings $settings -Action $action -Trigger $triggers -Force | Out-Null
Start-ScheduledTask -TaskName $task
