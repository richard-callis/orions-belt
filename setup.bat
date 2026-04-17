@echo off
REM Orion's Belt — Windows setup script
REM Run once after cloning: setup.bat
REM
REM To reset a broken venv (PowerShell):
REM   Remove-Item -Recurse -Force .venv
REM   .\setup.bat

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
if exist .venv (
    echo   .venv already exists — skipping creation.
    echo   To do a clean reinstall, run in PowerShell:
    echo     Remove-Item -Recurse -Force .venv
    echo     .\setup.bat
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
)

REM Activate
call .venv\Scripts\activate.bat

REM Upgrade pip first
echo [2/5] Upgrading pip...
python -m pip install --upgrade pip --quiet

REM Install core web framework first (fail fast if something is wrong)
echo [3/5] Installing core dependencies...
echo   Flask, SQLAlchemy, OpenAI client, utilities...
pip install ^
    flask flask-sqlalchemy flask-migrate sqlalchemy alembic ^
    openai requests httpx ^
    python-dotenv cryptography python-dateutil humanize bcrypt pillow
if errorlevel 1 (
    echo ERROR: Core install failed. Check your internet connection.
    pause
    exit /b 1
)

REM Install ML/NLP stack
echo [4/5] Installing NLP stack...
echo   PyTorch (CPU), transformers, sentence-transformers, presidio, spaCy
echo   NOTE: Models are NOT downloaded here — they download on first launch
echo         and are cached in the models\ folder (~580MB total).
echo.

REM PyTorch CPU-only (much smaller than CUDA build)
pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet
if errorlevel 1 (
    echo WARNING: PyTorch install failed. Trying standard index...
    pip install torch
)

pip install transformers sentence-transformers numpy
pip install presidio-analyzer presidio-anonymizer spacy

REM spaCy model (12MB, needed for presidio)
python -m spacy download en_core_web_sm
if errorlevel 1 (
    echo WARNING: spaCy model download failed.
    echo   Retry later with: .venv\Scripts\activate ^&^& python -m spacy download en_core_web_sm
)

REM Desktop launcher + Windows connectors
echo [5/5] Installing desktop launcher and connectors...
pip install pywebview pystray
pip install pywin32 pyodbc
if errorlevel 1 (
    echo WARNING: Some Windows-specific packages failed.
    echo   pywin32 ^(Outlook^) and pyodbc ^(SQL Server^) are optional.
)

REM Create local dirs
if not exist logs mkdir logs
if not exist models mkdir models

REM Download HuggingFace models
echo.
echo [+] Downloading AI models (~670MB total)...
echo     dslim/bert-base-NER          ~400MB  PII detection
echo     nli-deberta-v3-small         ~180MB  PHI judge
echo     all-MiniLM-L6-v2             ~90MB   Memory embeddings
echo.
python download_models.py
if errorlevel 1 (
    echo WARNING: Some models failed to download.
    echo   They will be retried on first launch.
    echo   Re-run manually: python download_models.py
)

REM Generate icon + create desktop shortcut
echo.
echo [+] Creating desktop shortcut...
python create_icon.py
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$projectDir = '%~dp0'.TrimEnd('\'); " ^
  "$desktop = [Environment]::GetFolderPath('Desktop'); " ^
  "$iconPath = Join-Path $projectDir 'app\static\img\icon.ico'; " ^
  "$vbsPath  = Join-Path $projectDir 'start_silent.vbs'; " ^
  "$lnk = Join-Path $desktop 'Orions Belt.lnk'; " ^
  "$shell = New-Object -ComObject WScript.Shell; " ^
  "$sc = $shell.CreateShortcut($lnk); " ^
  "$sc.TargetPath = 'wscript.exe'; " ^
  "$sc.Arguments  = \"\`\"$vbsPath\`\"\"; " ^
  "$sc.WorkingDirectory = $projectDir; " ^
  "$sc.Description = 'Orion''s Belt — Local AI Workbench'; " ^
  "if (Test-Path $iconPath) { $sc.IconLocation = $iconPath }; " ^
  "$sc.Save(); " ^
  "Write-Host '  Shortcut created: Orions Belt on Desktop'"

echo.
echo  ==========================================
echo   Setup complete!
echo  ==========================================
echo.
echo  Double-click "Orions Belt" on your Desktop to launch.
echo  (Or run run.bat from this folder)
echo.
pause
