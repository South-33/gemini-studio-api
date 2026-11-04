$ErrorActionPreference = 'Stop'

$startupName = 'Gemini Studio API'
$bootstrapTaskName = 'Gemini Studio API Bootstrap'
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder "$startupName.lnk"
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

$launcherPattern = [Regex]::Escape($launcher)
$launcherProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match '^python(w)?\.exe$' -and
            $_.CommandLine -match $launcherPattern
        }
)
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

if ($existingTask) {
    Stop-ScheduledTask -TaskName $startupName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $startupName -Confirm:$false
}

# Retire the legacy HKCU Run entry. On this production host it is also processed
# for OpenSSH logons, so it can unexpectedly relaunch the headed browser.
Remove-ItemProperty -Path $runKey -Name $startupName -ErrorAction SilentlyContinue

# Persistent autostart lives in Explorer's Startup folder. Explorer processes
# this shortcut for the real interactive desktop; OpenSSH session logons do not.
New-Item -ItemType Directory -Path $startupFolder -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"{0}"' -f $launcher
$shortcut.WorkingDirectory = $repoRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Starts Gemini Studio API in the interactive desktop session.'
$shortcut.Save()

if ($launcherProcesses.Count -gt 0 -and $apiHealthy) {
    Write-Host "Interactive startup shortcut configured and healthy: $startupName"
    Write-Host 'Leaving the live launcher process tree untouched.'
    exit 0
}

# A previously detached launcher can outlive the scheduled-task registration
# long enough to keep the single-instance mutex.  Stop any launcher that owns
# this repo before registering the new task so the replacement can start cleanly.
$launcherProcesses | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 750

$currentSessionId = (Get-Process -Id $PID).SessionId
if ($currentSessionId -eq 0) {
    # OpenSSH runs in session 0. Use a short-lived interactive task only as a
    # launch bridge so headed Chromium starts on the real desktop. The task
    # exits immediately after Start-Process and is then deleted; persistent
    # autostart remains only the Explorer Startup shortcut above.
    $bootstrapCommand = "Start-Process -FilePath '$pythonw' -ArgumentList '$launcher' -WorkingDirectory '$repoRoot' -WindowStyle Hidden"
    $bootstrapAction = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"$bootstrapCommand`""
    $bootstrapPrincipal = New-ScheduledTaskPrincipal `
        -UserId $currentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $bootstrapSettings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::FromMinutes(2)) `
        -MultipleInstances IgnoreNew

    Unregister-ScheduledTask -TaskName $bootstrapTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName $bootstrapTaskName `
        -Action $bootstrapAction `
        -Principal $bootstrapPrincipal `
        -Settings $bootstrapSettings `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $bootstrapTaskName
    Start-Sleep -Seconds 4
    Unregister-ScheduledTask -TaskName $bootstrapTaskName -Confirm:$false
} else {
    Start-Process `
        -FilePath $pythonw `
        -ArgumentList @($launcher) `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden
}
Start-Sleep -Seconds 3

Write-Host "Installed interactive startup shortcut: $shortcutPath"
Write-Host "User: $currentUser"
Write-Host "Launcher: $launcher"
Write-Host ''
Write-Host 'Explorer starts the launcher when the Windows desktop signs in. SSH logons do not trigger it.'
$winlogon = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
if ($winlogon.AutoAdminLogon -ne '1') {
    Write-Warning 'Windows auto-login is disabled. After a full reboot, sign in to this account before the API can start.'
}
