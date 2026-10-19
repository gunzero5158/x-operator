@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-local.ps1"
set "launcherExit=%errorlevel%"
if errorlevel 1 (
  echo.
  echo [x-operator] Restart did not finish. Please keep this window open and copy the error.
)
pause
exit /b %launcherExit%
