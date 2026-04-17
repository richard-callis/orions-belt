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
echo [1/5] Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment
    pause
    exit /b 1
)

REM Activate
call .venv\Scripts\activate.bat

REM Upgrade pip first
echo [2/5] Upgrading pip...
python -m pip install --upgrade pip --quiet

REM Install core web framework first (fail fast if something is wrong)
echo [3/5] Installing core dependencies (Flask, SQLAlchemy, openai)...
pip install flask flask-sqlalchemy flask-migrate sqlalchemy alembic openai requests httpx python-dotenv cryptography python-dateutil humanize bcrypt pillow
if errorlevel 1 (
    echo ERROR: Core install failed. Check your internet connection and Python version.
    pause
    exit /b 1
)

REM Install ML/NLP stack (CPU torch — no CUDA needed)
echo [4/5] Installing NLP stack (transformers, spacy, presidio)...
echo   This may take a few minutes — downloading ~500MB of models...
pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet
pip install transformers sentence-transformers numpy
pip install presidio-analyzer presidio-anonymizer spacy

REM Download spaCy model (small — 12MB)
echo   Downloading spaCy language model...
python -m spacy download en_core_web_sm
if errorlevel 1 (
    echo WARNING: spaCy model download failed. PII rule-based detection will be limited.
    echo   You can retry later with: .venv\Scripts\activate ^&^& python -m spacy download en_core_web_sm
)

REM Install Windows desktop packages
echo [5/5] Installing desktop launcher (pywebview, pystray) and connectors...
pip install pywebview pystray
pip install pywin32 pyodbc

REM Create local dirs
if not exist logs mkdir logs
if not exist models mkdir models

echo.
echo  Setup complete!
echo.
echo  To start Orion's Belt:
echo    Double-click run.bat
echo.
echo  Or from terminal:
echo    .venv\Scripts\activate
echo    python launch.py
echo.
pause
