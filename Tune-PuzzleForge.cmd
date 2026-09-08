@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\puzzleforge-local.ps1" -Tune %*
if errorlevel 1 pause
endlocal
