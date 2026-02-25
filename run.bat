@echo off
title GANTRY Watchdog
setlocal enabledelayedexpansion

:: Set colors (Gray background, White text)
color 0F

echo ====================================================
echo   GANTRY - The Script-to-API Bridge
echo ====================================================
echo.

:: Run the watchdog
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File ensure_gantry.ps1

echo.
echo ====================================================
echo   RECENT LOG ACTIVITY:
echo ====================================================
:: This tails the last 5 lines of the watchdog log for quick verification
powershell -Command "if (Test-Path logs/gantry_watchdog.log) { Get-Content logs/gantry_watchdog.log -Tail 5 } else { Write-Host 'No logs found.' }"
echo ====================================================
echo.
echo [System Idle] Press any key to close this window...
pause > nul