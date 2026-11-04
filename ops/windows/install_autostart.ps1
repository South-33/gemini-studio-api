$ErrorActionPreference = 'Stop'

$taskName = 'Gemini Studio API'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$launcher = Join-Path $repoRoot 'launcher.py'
$python = (& python -c 'import sys; print(sys.executable)').Trim()
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $launcher)) {
    throw "Could not find $launcher"
}
if (-not $python -or -not (Test-Path $python)) {
    throw "Could not resolve the real Python interpreter"
}

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# A previously detached launcher can outlive the scheduled-task registration
# long enough to keep the single-instance mutex.  Stop any launcher that owns
# this repo before registering the new task so the replacement can start cleanly.
$launcherPattern = [Regex]::Escape($launcher)
Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and
        $_.CommandLine -match $launcherPattern
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Milliseconds 750

# Gemini uses headed Chromium, so this task must run with the signed-in user's
# interactive token. Trigger from machine startup instead of user logon: an
# AtLogOn trigger also fires for OpenSSH logons under the same account and can
# tear down an in-flight headed browser. StartWhenAvailable lets Task Scheduler
# wait until the interactive user token exists after boot.
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-u `"$launcher`"" `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

# Task Scheduler only bootstraps the foreground launcher after startup. The
# launcher itself owns API/ngrok health and restarts; a second task-level
# restart policy can tear down an in-flight Gemini request.

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Runs Gemini Studio API, Chromium, and ngrok after Windows starts and the interactive user token is available.' `
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
Write-Host 'This task becomes due at Windows startup and starts when the interactive user token is available.'
$winlogon = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
if ($winlogon.AutoAdminLogon -ne '1') {
    Write-Warning 'Windows auto-login is disabled. After a full reboot, sign in to this account before the API can start.'
}
