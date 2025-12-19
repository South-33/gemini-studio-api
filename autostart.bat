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

:: Wait for API to initialize (single Gemini Web tab)
echo Waiting for initialization...
timeout /t 25 /nobreak >nul

:: Start Cloudflare Tunnel
echo [3/3] Starting Cloudflare Tunnel...
cloudflared tunnel --url http://localhost:8000

:: If cloudflared exits, keep window open to see errors
pause
