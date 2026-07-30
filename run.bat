@echo off
REM Orion's Belt — single entry point: installs if needed, then starts.
REM Double-click this to run the app, whether this is the first launch or the
REM hundredth — no need to remember to run setup.bat separately.

REM Change to the directory containing this script (works from anywhere)
cd /d "%~dp0"

if not exist .venv (
    echo Virtual environment not found — running first-time setup...
    echo.
    call "%~dp0setup.bat"
    if errorlevel 1 (
        echo Setup failed. See the messages above.
        exit /b 1
    )
    REM setup.bat ends with its own "pause" — this run continues straight on
    REM into starting the app once the user dismisses it.
)

call .venv\Scripts\activate.bat
python launch.py
