@echo off
cd /d "%~dp0"
python --version >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Installing now...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  echo.
  echo Python installed. Please close this window and double-click pc_setup.bat again.
  pause
  exit /b
)
python pc_setup.py
if errorlevel 1 pause
