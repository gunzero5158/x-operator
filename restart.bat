@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0scripts\restart-local.ps1"
if errorlevel 1 (
  echo.
  echo [x-operator] Restart did not finish. Please keep this window open and copy the error.
)
pause
