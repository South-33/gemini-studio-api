$ErrorActionPreference = 'Stop'

$startupName = 'Gemini Studio API'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$launcher = Join-Path $repoRoot 'launcher.py'
$python = (& python -c 'import sys; print(sys.executable)').Trim()
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $launcher)) {
    throw "Could not find $launcher"
}
if (-not $python -or -not (Test-Path $python)) {
    throw "Could not resolve the real Python interpreter"
}
if (-not (Test-Path $pythonw)) {
    $pythonw = $python
}

$runCommand = '"{0}" "{1}"' -f $pythonw, $launcher
$launcherPattern = [Regex]::Escape($launcher)
$launcherProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match '^python(w)?\.exe$' -and
            $_.CommandLine -match $launcherPattern
        }
)
$currentRunCommand = $null
if (Test-Path $runKey) {
    $currentRunCommand = (Get-ItemProperty -Path $runKey -Name $startupName -ErrorAction SilentlyContinue).$startupName
}
$existingTask = Get-ScheduledTask -TaskName $startupName -ErrorAction SilentlyContinue

# Re-running the installer is a normal deploy/repair operation. If production
# already has the desired Run entry, no legacy task, and a healthy launcher,
# leave the live browser process tree alone. This keeps concurrent maintenance
# sessions from interrupting an in-flight Gemini request.
$apiHealthy = $false
try {
    $health = Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 5
    $apiHealthy = $health.ready -eq $true
} catch {
    $apiHealthy = $false
}

if (-not $existingTask -and $currentRunCommand -eq $runCommand -and $launcherProcesses.Count -gt 0 -and $apiHealthy) {
    Write-Host "Interactive startup entry already configured and healthy: $startupName"
    Write-Host 'Leaving the live launcher process tree untouched.'
    exit 0
}

if ($existingTask) {
    Stop-ScheduledTask -TaskName $startupName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $startupName -Confirm:$false
}

# A previously detached launcher can outlive the scheduled-task registration
# long enough to keep the single-instance mutex.  Stop any launcher that owns
# this repo before registering the new task so the replacement can start cleanly.
$launcherProcesses | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 750

# Gemini uses headed Chromium, so autostart must happen only when Explorer has
# created the real interactive desktop. HKCU Run is intentionally used instead
# of Task Scheduler: SSH logons do not execute this key, and there is no task
# service that can stop/relaunch the process tree during an active request.
New-Item -Path $runKey -Force | Out-Null
Set-ItemProperty -Path $runKey -Name $startupName -Value $runCommand

Start-Process `
    -FilePath $pythonw `
    -ArgumentList @($launcher) `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden
Start-Sleep -Seconds 3

Write-Host "Installed interactive startup entry: $startupName"
Write-Host "User: $currentUser"
Write-Host "Launcher: $launcher"
Write-Host "Command: $runCommand"
Write-Host ''
Write-Host 'The launcher starts when the Windows desktop signs in. SSH logons do not trigger it.'
$winlogon = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
if ($winlogon.AutoAdminLogon -ne '1') {
    Write-Warning 'Windows auto-login is disabled. After a full reboot, sign in to this account before the API can start.'
}
