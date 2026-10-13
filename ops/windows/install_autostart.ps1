$ErrorActionPreference = 'Stop'

$taskName = 'Gemini Studio API'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$launcher = Join-Path $repoRoot 'launcher.py'
$python = (Get-Command python -ErrorAction Stop).Source
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $launcher)) {
    throw "Could not find $launcher"
}

# Gemini uses headed Chromium, so this task must run inside the signed-in user's
# interactive desktop session. A machine-startup/SYSTEM task would launch the
# browser in session 0 where it cannot be used by the automation.
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-u `"$launcher`"" `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Runs Gemini Studio API, Chromium, and ngrok whenever the server user signs in.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 2

$task = Get-ScheduledTask -TaskName $taskName
$info = $task | Get-ScheduledTaskInfo

Write-Host "Installed scheduled task: $taskName"
Write-Host "User: $currentUser"
Write-Host "State: $($task.State)"
Write-Host "Last result: $($info.LastTaskResult)"
Write-Host "Launcher: $launcher"
Write-Host ''
Write-Host 'This task starts at Windows sign-in because Chromium needs an interactive desktop.'
$winlogon = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
if ($winlogon.AutoAdminLogon -ne '1') {
    Write-Warning 'Windows auto-login is disabled. After a full reboot, sign in to this account before the API can start.'
}
