@echo off
title Gemini Studio API - Auto Start
echo ==========================================
echo   Gemini Studio - Local Bridge Starter
echo ==========================================

:: Navigate to script directory
cd /d "%~dp0"

:: Wait for network (important after boot)
echo [1/3] Waiting for network...
timeout /t 10 /nobreak >nul

:: Start the API in background
echo [2/3] Starting API server...
start /min "GeminiAPI" cmd /c "python main_bridge.py"

:: Wait for API to initialize
timeout /t 15 /nobreak >nul

:: Start Cloudflare Tunnel
echo [3/3] Starting Cloudflare Tunnel...
cloudflared tunnel --url http://localhost:8000

:: If cloudflared exits, keep window open to see errors
pause
