# ==============================================================================
# Virtual Display Driver Auto-Installer for Windows 10/11 Server
# Creates a synthetic 1920x1080 virtual display monitor for headless/RDP servers
# ==============================================================================

# Ensure Administrator privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[!] Please run PowerShell as Administrator to install the Virtual Display Driver." -ForegroundColor Red
    Exit 1
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Virtual Display Driver Setup (Synthetic Screen for RDP)   " -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$driverDir = "C:\VirtualDisplayDriver"
if (-not (Test-Path $driverDir)) {
    New-Item -ItemType Directory -Path $driverDir | Out-Null
}

$zipPath = "$driverDir\VirtualDisplayDriver.zip"

Write-Host "`n[1/3] Fetching latest Virtual Display Driver from GitHub..." -ForegroundColor Yellow
try {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/itsmikethetech/Virtual-Display-Driver/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -like "*.zip" } | Select-Object -First 1
    
    if ($asset) {
        Write-Host "Downloading $($asset.name)..." -ForegroundColor Green
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath
    } else {
        Write-Host "[!] Could not locate zip file in latest release." -ForegroundColor Red
        Exit 1
    }
} catch {
    Write-Host "[!] Error downloading driver package: $_" -ForegroundColor Red
    Exit 1
}

Write-Host "[2/3] Extracting driver package to $driverDir..." -ForegroundColor Yellow
Expand-Archive -Path $zipPath -DestinationPath $driverDir -Force

Write-Host "[3/3] Driver downloaded to $driverDir." -ForegroundColor Yellow
Write-Host "To complete driver installation:" -ForegroundColor Cyan
Write-Host "  1. Open folder: $driverDir" -ForegroundColor White
Write-Host "  2. Right-click 'nefconw.exe' or the installer .bat / setup script and 'Run as Administrator'" -ForegroundColor White
Write-Host "  3. Windows will immediately detect a synthetic 1920x1080 display!" -ForegroundColor Green

Write-Host "`n✅ Done! Check $driverDir for files." -ForegroundColor Green
