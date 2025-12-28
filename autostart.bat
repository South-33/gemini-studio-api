@echo off
title Gemini Studio API - Auto Start
echo ==========================================
echo   Gemini Studio API Starter
echo ==========================================

:: Navigate to script directory
cd /d "%~dp0"

:: Wait for network (important after boot)
echo [1/3] Waiting for network...
timeout /t 10 /nobreak >nul

:: Start the API in background
echo [2/3] Starting API server...
start "GeminiAPI" /min cmd /c "python main.py"

:: Wait for API with timeout (max 60 seconds)
echo Waiting for API to be ready (max 60s)...
set /a counter=0
set /a max_tries=20

:check_api
set /a counter+=1
echo   Attempt %counter%/%max_tries%...

powershell -Command "(Invoke-WebRequest -Uri 'http://localhost:8000/health' -UseBasicParsing -TimeoutSec 3).StatusCode" 2>nul | findstr "200" >nul

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
echo [3/3] Waiting 10s for browser to stabilize...
timeout /t 10 /nobreak >nul

:: Start ngrok Tunnel in BACKGROUND (minimized)
:: Using static domain for persistent URL (requires ngrok account)
echo.
echo ==========================================
echo   Starting ngrok Tunnel + Keepalive
echo ==========================================
:: Replace YOUR_STATIC_DOMAIN with your ngrok static domain (e.g., my-gemini-api.ngrok-free.app)
:: Get your free static domain at: https://dashboard.ngrok.com/cloud-edge/domains
start "NgrokTunnel" /min cmd /c "ngrok http 8000 --domain=YOUR_STATIC_DOMAIN"

:: Give tunnel time to register
timeout /t 5 /nobreak >nul

:: Keepalive loop (runs forever, monitors tunnel health)
echo [Keepalive] Monitoring tunnel every 5 minutes...
echo.

:keepalive
:: Check if ngrok process is still running
tasklist /FI "IMAGENAME eq ngrok.exe" 2>nul | find /I "ngrok.exe" >nul
if %errorlevel% neq 0 (
    echo [Tunnel] ⚠️ Tunnel process died! Restarting...
    start "NgrokTunnel" /min cmd /c "ngrok http 8000 --domain=YOUR_STATIC_DOMAIN"
    timeout /t 5 /nobreak >nul
)

:: Ping health endpoint to verify API is responsive
powershell -Command "(Invoke-WebRequest -Uri 'http://localhost:8000/health' -UseBasicParsing -TimeoutSec 5).StatusCode" 2>nul | findstr "200" >nul
if %errorlevel% equ 0 (
    echo [%time%] ✅ Keepalive OK
) else (
    echo [%time%] ⚠️ Keepalive failed (API might be busy)
)

:: Wait 5 minutes
timeout /t 300 /nobreak >nul
goto keepalive
