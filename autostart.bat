@echo off
title Gemini Studio API - Auto Start
echo ==========================================
echo   Gemini Studio API Starter
echo ==========================================

:: Navigate to script directory
cd /d "%~dp0"

:: Load PORT and NGROK_DOMAIN from .env file
set PORT=8000
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="PORT" set PORT=%%b
    if "%%a"=="NGROK_DOMAIN" set NGROK_DOMAIN=%%b
)

:: Normalize ngrok domain so .env can contain either the bare host or a full URL
set "NGROK_DOMAIN=%NGROK_DOMAIN:https://=%"
set "NGROK_DOMAIN=%NGROK_DOMAIN:http://=%"
if "%NGROK_DOMAIN:~-1%"=="/" set "NGROK_DOMAIN=%NGROK_DOMAIN:~0,-1%"

echo [Config] PORT=%PORT%
echo [Config] NGROK_DOMAIN=%NGROK_DOMAIN%

if not defined NGROK_DOMAIN (
    echo [ERROR] NGROK_DOMAIN not set in .env file!
    pause
    exit /b 1
)

:: Kill any orphaned processes on this port
echo [1/3] Killing any orphaned process on port %PORT%...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo   Found PID %%p on port %PORT%, killing...
    taskkill /F /PID %%p 2>nul
)
timeout /t 3 /nobreak >nul

:: Start the API minimized (prevents Windows Quick Edit mode freezing)
echo [2/3] Starting API server on port %PORT%...
start "GeminiAPI" /min cmd /c "set PYTHONUNBUFFERED=1 && python -u main.py"

:: Start ngrok in a visible window so tunnel errors are easy to inspect
echo [3/3] Starting ngrok tunnel on port %PORT%...
echo Using domain: %NGROK_DOMAIN%
start "NgrokTunnel" cmd /k "ngrok http %PORT% --domain=%NGROK_DOMAIN%"

echo.
echo ==========================================
echo   ✅ All services started!
echo   API: http://localhost:%PORT%
echo   Tunnel: https://%NGROK_DOMAIN%
echo ==========================================
echo.
echo [Monitor] Watching for process crashes...

:monitor
timeout /t 60 /nobreak >nul

:: Check if ngrok is still running
tasklist /FI "IMAGENAME eq ngrok.exe" 2>nul | find /I "ngrok.exe" >nul
if %errorlevel% neq 0 (
    echo [%time%] ⚠️ Tunnel died! Restarting...
    start "NgrokTunnel" cmd /k "ngrok http %PORT% --domain=%NGROK_DOMAIN%"
    timeout /t 5 /nobreak >nul
)

:: Check if API port is still listening (more reliable than checking python.exe)
netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul
if %errorlevel% neq 0 (
    echo [%time%] ⚠️ API port not listening! Restarting...
    start "GeminiAPI" /min cmd /c "set PYTHONUNBUFFERED=1 && python -u main.py"
    timeout /t 5 /nobreak >nul
)

goto monitor
