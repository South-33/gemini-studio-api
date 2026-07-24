@echo off
title Disconnect RDP and Keep Screen Active
echo ============================================================
echo   Redirecting RDP Session to Local Console (Screen Active)
echo ============================================================
echo.

:: Get current session ID and transfer to console
for /f "skip=1 tokens=3" %%s in ('query user %username%') do (
    echo Transferring Session ID %%s to Console...
    tscon %%s /dest:console
)

if %errorlevel% neq 0 (
    echo.
    echo [NOTE] If permission was denied, right-click this script and "Run as Administrator".
    pause
)
