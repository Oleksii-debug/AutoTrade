[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,

    [Parameter(Mandatory = $true)]
    [string]$StateDirectory,

    [string]$PythonExecutable,

    [string]$TaskName = 'AutoTrade Background Runtime',

    [string]$RunValueName = 'AutoTradeBackgroundRuntime',

    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

if ($Remove) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled watchdog task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Remove-ItemProperty -LiteralPath $runKey -Name $RunValueName -ErrorAction SilentlyContinue
    }
    return
}

$repo = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$manifest = Join-Path $repo 'packaging\windows\autotrade-runtime-manifest.json'
$watchdog = Join-Path $repo 'tools\run_autotrade_process_watchdog.py'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf) -or
    -not (Test-Path -LiteralPath $watchdog -PathType Leaf)) {
    throw 'The repository does not contain the watchdog and its manifest.'
}

foreach ($path in @($repo, $RuntimeRoot, $StateDirectory)) {
    if ($path.Contains('"')) {
        throw 'Watchdog paths must not contain quote characters.'
    }
}

$runtime = (New-Item -ItemType Directory -Path $RuntimeRoot -Force).FullName
$state = (New-Item -ItemType Directory -Path $StateDirectory -Force).FullName
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $python = (Get-Command python -ErrorAction Stop).Source
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonw -PathType Leaf) {
        $python = $pythonw
    }
} else {
    $python = (Resolve-Path -LiteralPath $PythonExecutable).Path
}

$arguments = @(
    '"' + $watchdog + '"',
    '--manifest', '"' + $manifest + '"',
    '--runtime-root', '"' + $runtime + '"',
    '--state-dir', '"' + $state + '"',
    '--respect-stop-marker'
) -join ' '

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $repo
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$recoveryTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -StartWhenAvailable

if ($PSCmdlet.ShouldProcess($TaskName, 'Install current-user watchdog startup')) {
    try {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger @($logonTrigger, $recoveryTrigger) `
            -Settings $settings `
            -Description 'Restarts AutoTrade public data, ZERO learning, and attention processes without trading authority.' `
            -RunLevel Limited `
            -Force `
            -ErrorAction Stop | Out-Null
        Remove-ItemProperty -LiteralPath $runKey -Name $RunValueName -ErrorAction SilentlyContinue
    } catch {
        # Some managed Windows accounts deny Task Scheduler registration even
        # for a Limited current-user task. HKCU Run is a non-admin, per-user
        # fallback and preserves the same IgnoreNew behavior through the
        # watchdog's single-owner state lock.
        New-Item -Path $runKey -Force | Out-Null
        $command = '"' + $python + '" ' + $arguments
        Set-ItemProperty -LiteralPath $runKey -Name $RunValueName -Value $command
    }
}
