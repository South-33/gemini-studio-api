@echo off
echo ==========================================
echo   Gemini Studio - Local Bridge Starter
echo ==========================================

REM 1. Kill any existing Chrome debugging instances
taskkill /F /IM chrome.exe /FI "WINDOWTITLE eq *API-Session*" >nul 2>&1

REM 2. Launch Chrome with Remote Debugging
echo [1/2] Launching Chrome with --remote-debugging-port=9222...
start "API-Session" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\Google\Chrome\API-Session"

REM 3. Wait for Chrome to initialize
echo Waiting 5 seconds for Chrome...
timeout /t 5 /nobreak >nul

REM 4. Launch the Python API
echo [2/2] Starting main_bridge.py...
python main_bridge.py

pause
