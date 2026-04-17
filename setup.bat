@echo off
REM Orion's Belt — Windows setup script
REM Run once after cloning: setup.bat

echo.
echo  * * *  Orion's Belt Setup  * * *
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)

REM Create venv
echo [1/4] Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment
    pause
    exit /b 1
)

REM Activate and install
echo [2/4] Installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt

REM Download spaCy model
echo [3/4] Downloading spaCy language model...
python -m spacy download en_core_web_lg

REM Create logs directory
echo [4/4] Creating local directories...
if not exist logs mkdir logs
if not exist models mkdir models

echo.
echo  Setup complete!
echo.
echo  To start Orion's Belt:
echo    .venv\Scripts\activate
echo    python launch.py
echo.
echo  Or just double-click: run.bat
echo.
pause
