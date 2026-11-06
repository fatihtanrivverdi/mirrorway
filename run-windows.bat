@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Runs the Windows side from this folder (for development; installing with install-windows.bat is recommended).

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH" during the installation^).
  pause
  exit /b 1
)

if not exist ".venv-win\Scripts\python.exe" (
  echo Setting up for the first time, this may take 1-2 minutes...
  %PY% -m venv .venv-win || ( echo Could not create the virtual environment & pause & exit /b 1 )
)
if not exist ".venv-win\.installed" (
  ".venv-win\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-windows.txt || ( echo Could not install the packages & pause & exit /b 1 )
  echo ok> ".venv-win\.installed"
)

echo Starting Mirrorway. Look for the Mirrorway icon in the system tray (next to the clock).
start "" ".venv-win\Scripts\pythonw.exe" -m mirrorway.win %*
timeout /t 3 >nul
