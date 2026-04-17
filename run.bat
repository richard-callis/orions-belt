@echo off
REM Orion's Belt — Quick launch
REM Double-click this to start the app

REM Change to the directory containing this script (works from anywhere)
cd /d "%~dp0"

if not exist .venv (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python launch.py
