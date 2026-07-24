# ==============================================================================
# Virtual Display Driver & RDP Auto-Keep-Alive Installer for Windows 10/11
# ==============================================================================

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[!] Error: Administrator privileges required!" -ForegroundColor Red
    Write-Host "Please right-click PowerShell and select 'Run as Administrator'." -ForegroundColor Yellow
    Exit 1
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Installing Synthetic Display Driver & Auto Keep-Alive    " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$driverDir = "C:\VirtualDisplayDriver"
if (-not (Test-Path $driverDir)) {
    New-Item -ItemType Directory -Path $driverDir | Out-Null
}

$zipPath = "$driverDir\VirtualDisplayDriver.zip"

# Step 1: Download latest driver
Write-Host "`n[1/4] Fetching Virtual Display Driver from GitHub..." -ForegroundColor Yellow
try {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/itsmikethetech/Virtual-Display-Driver/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -like "*.zip" } | Select-Object -First 1
    
    if ($asset) {
        Write-Host "Downloading $($asset.name)..." -ForegroundColor Green
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath
    } else {
        Write-Host "[!] Could not locate driver zip file in releases." -ForegroundColor Red
        Exit 1
    }
} catch {
    Write-Host "[!] Failed to download driver package: $_" -ForegroundColor Red
    Exit 1
}

# Step 2: Unzip driver
Write-Host "[2/4] Extracting driver files to $driverDir..." -ForegroundColor Yellow
Expand-Archive -Path $zipPath -DestinationPath $driverDir -Force

# Step 3: Install Driver into Windows Driver Store using Pnputil
Write-Host "[3/4] Registering Indirect Display Driver with Windows..." -ForegroundColor Yellow
$infFiles = Get-ChildItem -Path $driverDir -Recurse -Filter "*.inf"
if ($infFiles) {
    foreach ($inf in $infFiles) {
        Write-Host "Installing driver INF: $($inf.FullName)" -ForegroundColor Green
        pnputil.exe /add-driver $inf.FullName /install
    }
} else {
    Write-Host "[!] No INF file found in extracted directory." -ForegroundColor Yellow
}

# Step 4: Create Scheduled Task for Auto RDP Console Redirection
Write-Host "[4/4] Setting up Windows Scheduled Task for RDP Keep-Alive..." -ForegroundColor Yellow
$taskName = "KeepScreenActiveOnRDPDisconnect"
$scriptPath = "$PSScriptRoot\disconnect_rdp.bat"

if (-not (Test-Path $scriptPath)) {
    $scriptPath = "C:\Users\nyxy\Desktop\gemini-studio-api\disconnect_rdp.bat"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -ErrorAction Stop | Out-Null
    Write-Host "Scheduled Task '$taskName' successfully registered!" -ForegroundColor Green
} catch {
    Write-Host "[!] Warning: Could not register scheduled task automatically: $_" -ForegroundColor Yellow
}

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host "  ✅ Virtual Display Driver & Auto Keep-Alive Installed!    " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "1. A synthetic 1920x1080 display is now active in Windows Display Settings." -ForegroundColor White
Write-Host "2. Running 'disconnect_rdp.bat' will keep GUI rendering alive whenever you leave RDP." -ForegroundColor White
