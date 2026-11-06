@echo off
cd /d "%~dp0"
if not exist "%~dp0install-windows.ps1" (
  echo First right-click the zip file and choose "Extract All", then run this file from the extracted folder.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
