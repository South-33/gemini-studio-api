@echo off
title Gemini Studio API - Auto Start
echo ==========================================
echo   Gemini Studio API Starter
echo ==========================================

:: Navigate to script directory
cd /d "%~dp0"

:: Wait for network (important after boot)
echo [1/3] Waiting for network...
timeout /t 5 /nobreak >nul

:: Start the API in background
echo [2/3] Starting API server...
start "GeminiAPI" cmd /c "python main.py"

:: Dynamic wait for API to be ready
echo Waiting for API to be ready...
:check_api
powershell -Command "try { $response = Invoke-WebRequest -Uri 'http://localhost:8000/health' -UseBasicParsing -ErrorAction Ignore; if ($response.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if %errorlevel% neq 0 (
    timeout /t 2 /nobreak >nul
    goto check_api
)
echo [Server] ✅ API is online!

:: Start Cloudflare Tunnel
echo [3/3] Starting Cloudflare Tunnel...
cloudflared tunnel --url http://localhost:8000

:: If cloudflared exits, keep window open to see errors
pause
