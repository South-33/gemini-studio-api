@echo off
title Gemini Studio API - Auto Start
echo ==========================================
echo   Gemini Studio API Starter
echo ==========================================

:: Navigate to script directory
cd /d "%~dp0"

:: Load PORT and NGROK_DOMAIN from .env file
set PORT=8001
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="PORT" set PORT=%%b
    if "%%a"=="NGROK_DOMAIN" set NGROK_DOMAIN=%%b
)
echo [Config] PORT=%PORT%

if not defined NGROK_DOMAIN (
    echo [ERROR] NGROK_DOMAIN not set in .env file!
    echo Add: NGROK_DOMAIN=your-domain.ngrok-free.app
    pause
    exit /b 1
)

:: Check if API is ALREADY running (skip startup if so)
echo [1/4] Checking if API already running on port %PORT%...
powershell -Command "(Invoke-WebRequest -Uri 'http://localhost:%PORT%/health' -UseBasicParsing -TimeoutSec 2).StatusCode" 2>nul | findstr "200" >nul
if %errorlevel% equ 0 (
    echo [Server] ✅ API already running! Skipping startup...
    goto api_ready
)

:: Kill any orphaned Python process on this port
echo [2/4] Killing any orphaned process on port %PORT%...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo   Found PID %%p on port %PORT%, killing...
    taskkill /F /PID %%p 2>nul
)
timeout /t 2 /nobreak >nul

:: Start the API
echo [3/4] Starting API server on port %PORT%...
start "GeminiAPI" /min cmd /c "python main.py"

:: Wait for API with timeout (max 60 seconds)
echo Waiting for API to be ready (max 60s)...
set /a counter=0
set /a max_tries=20

:check_api
set /a counter+=1
echo   Attempt %counter%/%max_tries%...

powershell -Command "(Invoke-WebRequest -Uri 'http://localhost:%PORT%/health' -UseBasicParsing -TimeoutSec 3).StatusCode" 2>nul | findstr "200" >nul

if %errorlevel% equ 0 (
    echo [Server] ✅ API is online!
    goto api_ready
)

if %counter% geq %max_tries% (
    echo [Server] ⚠️ Health check timed out, proceeding anyway...
    goto api_ready
)

timeout /t 3 /nobreak >nul
goto check_api

:api_ready
echo [4/4] Waiting 10s for browser to stabilize...
timeout /t 10 /nobreak >nul

:: Start ngrok Tunnel in BACKGROUND (minimized)
:: Using static domain for persistent URL (requires ngrok account)
echo.
echo ==========================================
echo   Starting ngrok Tunnel
echo ==========================================
:: Domain is loaded from .env file (NGROK_DOMAIN)
echo Using domain: %NGROK_DOMAIN%
start "NgrokTunnel" /min cmd /c "ngrok http %PORT% --domain=%NGROK_DOMAIN%"

:: Give tunnel time to register
timeout /t 5 /nobreak >nul

:: Monitor loop (process crash detection only, no HTTP pings)
:: ngrok has no idle timeout, so keepalive pings are unnecessary
echo [Monitor] Watching for tunnel crashes (no keepalive needed)...
echo.

:monitor
:: Check if ngrok process is still running
tasklist /FI "IMAGENAME eq ngrok.exe" 2>nul | find /I "ngrok.exe" >nul
if %errorlevel% neq 0 (
    echo [%time%] ⚠️ Tunnel process died! Restarting...
    start "NgrokTunnel" /min cmd /c "ngrok http %PORT% --domain=%NGROK_DOMAIN%"
    timeout /t 10 /nobreak >nul
)

:: Check every 60 seconds (no HTTP requests, just process check)
timeout /t 60 /nobreak >nul
goto monitor
