@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Troubleshooting: runs in a console window with a detailed log.
if not exist ".venv-win\Scripts\python.exe" ( echo Run run-windows.bat first. & pause & exit /b 1 )
".venv-win\Scripts\python.exe" -m mirrorway.win --debug %*
pause
