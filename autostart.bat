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

:: Activate virtual environment and start API
echo [2/3] Starting API server...
start "GeminiAPI" cmd /k "cd /d %~dp0 && call ..\..\..\env\Scripts\activate.bat && python main.py"

:: Wait for API to initialize
echo Waiting 20 seconds for API to start...
timeout /t 20 /nobreak >nul

:: Start Cloudflare Tunnel
echo [3/3] Starting Cloudflare Tunnel...
cloudflared tunnel --url http://localhost:8000

:: If cloudflared exits, keep window open to see errors
pause
