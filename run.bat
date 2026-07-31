@echo off
REM Orion's Belt — Windows entry point.
REM Installs (first run only) then starts the app, every time. All the actual
REM install/start logic lives in install.py — this just picks a python and
REM hands off to it. Double-click this to run the app.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)

python install.py
if errorlevel 1 (
    echo.
    echo Something went wrong. See the messages above.
    pause
    exit /b 1
)
