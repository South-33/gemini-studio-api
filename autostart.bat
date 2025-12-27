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
start "GeminiAPI" cmd /c "python main.py"

:: Wait for API with timeout (max 60 seconds)
echo Waiting for API to be ready (max 60s)...
set /a counter=0
set /a max_tries=20

:check_api
set /a counter+=1
echo   Attempt %counter%/%max_tries%...

:: Simple curl-style check using PowerShell
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
:: Extra wait for browser to fully initialize
echo Waiting 10s for browser to stabilize...
timeout /t 10 /nobreak >nul

:: Start Cloudflare Tunnel with auto-restart
:tunnel_start
echo.
echo [3/3] Starting Cloudflare Tunnel...
echo ==========================================
cloudflared tunnel --url http://localhost:8000

:: If cloudflared exits, restart it
echo.
echo [Tunnel] ⚠️ Tunnel exited. Restarting in 5s...
timeout /t 5 /nobreak >nul
goto tunnel_start
