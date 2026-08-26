@echo off
title Gemini Studio API - Auto Start
echo ==========================================
echo   Gemini Studio API Starter
echo ==========================================

:: Navigate to script directory
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "API_LOG=logs\server.log"

:: Load PORT and NGROK_DOMAIN from .env file
set PORT=8000
for /f "tokens=1,* delims==" %%a in (.env) do (
    if "%%a"=="PORT" set PORT=%%b
    if "%%a"=="NGROK_DOMAIN" set NGROK_DOMAIN=%%b
    if "%%a"=="DISCORD_WEBHOOK" set DISCORD_WEBHOOK=%%b
    if "%%a"=="DISCORD_USER_ID" set DISCORD_USER_ID=%%b
)

:: Normalize ngrok domain so .env can contain either the bare host or a full URL
set "NGROK_DOMAIN=%NGROK_DOMAIN:https://=%"
set "NGROK_DOMAIN=%NGROK_DOMAIN:http://=%"
if "%NGROK_DOMAIN:~-1%"=="/" set "NGROK_DOMAIN=%NGROK_DOMAIN:~0,-1%"

echo [Config] PORT=%PORT%
echo [Config] NGROK_DOMAIN=%NGROK_DOMAIN%
set "NGROK_CMD=ngrok http %PORT% --domain=%NGROK_DOMAIN%"

if not defined NGROK_DOMAIN (
    echo [ERROR] NGROK_DOMAIN not set in .env file!
    pause
    exit /b 1
)

:: Kill any orphaned processes on this port
echo [1/3] Killing any orphaned process on port %PORT%...
call :stop_api

:: Start both services in this console. Output goes to files, so restarts do
:: not create extra terminal windows.
echo [2/3] Starting API server on port %PORT%...
call :start_api
call :verify_api_startup
if errorlevel 1 (
    call :notify_discord "Gemini API Startup Failure" "Python exited or no Gemini browser worker became ready. Check logs\server.log on the server."
    echo [FATAL] API failed to become ready. Recent output:
    powershell -NoProfile -Command "if (Test-Path $env:API_LOG) { Get-Content $env:API_LOG -Tail 40 }"
    call :stop_api
    pause
    exit /b 1
)

echo [3/3] Starting ngrok tunnel on port %PORT%...
echo Using domain: %NGROK_DOMAIN%
call :ensure_ngrok
if errorlevel 1 exit /b 1

echo.
echo ==========================================
echo   All services started.
echo   API: http://localhost:%PORT%
echo   Tunnel: https://%NGROK_DOMAIN%
echo ==========================================
echo.
echo [Monitor] Watching for process crashes...

:monitor
timeout /t 60 /nobreak >nul

:: Check browser-worker readiness, not merely whether the API port is open.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/health' -TimeoutSec 3; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>nul
if errorlevel 1 (
    echo [%time%] API or Gemini worker unhealthy. Restarting...
    call :stop_api
    call :start_api
    call :verify_api_startup
    if errorlevel 1 (
        echo [%time%] API restart failed. See %API_LOG%.
        call :notify_discord "Gemini API Restart Failure" "The health check failed and no Gemini browser worker became ready after restart. Check logs\server.log."
    )
)

:: Reuse a healthy public tunnel; replace it when the public health check fails.
call :ensure_ngrok
if errorlevel 1 exit /b 1

goto monitor

:stop_api
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo   Found PID %%p on port %PORT%, killing...
    taskkill /F /PID %%p 2>nul
)
timeout /t 3 /nobreak >nul
exit /b 0

:start_api
echo.>>"%API_LOG%"
echo ============================================================>>"%API_LOG%"
echo API launch %date% %time%>>"%API_LOG%"
start "" /b cmd /c "set PYTHONUNBUFFERED=1&& python -u main.py 1>>logs\server.log 2>&1"
exit /b 0

:verify_api_startup
for /l %%i in (1,1,90) do (
    powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/health' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>nul
    if not errorlevel 1 exit /b 0
    timeout /t 1 /nobreak >nul
)
exit /b 1

:start_ngrok
echo.>>"logs\ngrok.log"
echo ============================================================>>"logs\ngrok.log"
echo Tunnel launch %date% %time%>>"logs\ngrok.log"
start "" /b cmd /c "%NGROK_CMD% 1>>logs\ngrok.log 2>&1"
exit /b 0

:ensure_ngrok
call :public_health
if not errorlevel 1 (
    exit /b 0
)
echo [%time%] Tunnel unhealthy; restarting...
taskkill /F /IM ngrok.exe >nul 2>nul
call :start_ngrok
call :verify_ngrok_startup
if errorlevel 1 exit /b 1
exit /b 0

:verify_ngrok_startup
for /l %%i in (1,1,20) do (
    call :public_health
    if not errorlevel 1 exit /b 0
    timeout /t 1 /nobreak >nul
)
call :notify_discord "Gemini API Tunnel Failure" "The public health check never became ready. Check logs\ngrok.log. Common causes are missing auth, an unverified account, or a domain owned by another account."
echo [FATAL] Public tunnel failed to become ready.
echo [FATAL] Check logs\ngrok.log for the real error.
echo [FATAL] Common causes: missing auth token, unverified ngrok account, or a domain from a different account.
taskkill /F /IM ngrok.exe >nul 2>nul
pause
exit /b 1

:public_health
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'https://%NGROK_DOMAIN%/health' -Headers @{'ngrok-skip-browser-warning'='1'} -TimeoutSec 4; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>nul
exit /b %errorlevel%

:notify_discord
if not defined DISCORD_WEBHOOK exit /b 0
set "DISCORD_TITLE=%~1"
set "DISCORD_MESSAGE=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$payload = @{ embeds = @(@{ title = $env:DISCORD_TITLE; description = $env:DISCORD_MESSAGE; color = 16711680; footer = @{ text = 'Gemini Studio API' } }) }; if ($env:DISCORD_USER_ID) { $payload.content = '<@' + $env:DISCORD_USER_ID + '>' }; $json = $payload | ConvertTo-Json -Depth 6; Invoke-RestMethod -Method Post -Uri $env:DISCORD_WEBHOOK -ContentType 'application/json' -Body $json | Out-Null" >nul 2>nul
exit /b 0
