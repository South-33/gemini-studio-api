@echo off
title Gemini Studio API
cd /d "%~dp0"
python -u launcher.py
exit /b %errorlevel%
