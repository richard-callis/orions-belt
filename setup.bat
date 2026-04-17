@echo off
REM Orion's Belt — Windows setup script
REM Run once after cloning: setup.bat
REM
REM To reset a broken venv (PowerShell):
REM   Remove-Item -Recurse -Force .venv
REM   .\setup.bat

setlocal EnableDelayedExpansion

echo.
echo  * * *  Orion's Belt Setup  * * *
echo.

REM ── SSL bypass state (0 = not yet approved, 1 = approved for this session) ──
set SSL_BYPASS=0

REM ── Check Python ─────────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)

REM ── Create venv ──────────────────────────────────────────────────────────────
echo [1/5] Creating virtual environment...
if exist .venv (
    echo   .venv already exists — reusing it.
    echo   To start fresh, run in PowerShell:
    echo     Remove-Item -Recurse -Force .venv
    echo     .\setup.bat
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

REM ── Upgrade pip ──────────────────────────────────────────────────────────────
echo [2/5] Upgrading pip...
call :pip_install --upgrade pip --quiet
if errorlevel 1 (
    echo ERROR: pip upgrade failed.
    pause
    exit /b 1
)

REM ── Core dependencies ─────────────────────────────────────────────────────────
echo [3/5] Installing core dependencies...
call :pip_install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Core install failed.
    pause
    exit /b 1
)

REM ── ML / NLP stack ───────────────────────────────────────────────────────────
echo [4/5] Installing NLP stack ^(PyTorch, transformers, presidio, spaCy^)...
echo   NOTE: Models download on first launch, cached in models\ ^(~670MB^).
echo.

REM PyTorch — always install the CPU-only build on Windows.
REM The default PyPI torch is a CUDA build whose c10.dll fails to initialise
REM on machines without a compatible CUDA runtime. Uninstall first so pip
REM doesn't skip re-installation if a broken CUDA build is already present.
echo   Removing any existing torch install...
pip uninstall torch -y --quiet 2>nul
echo   Installing PyTorch CPU-only build...
call :pip_install torch --index-url https://download.pytorch.org/whl/cpu --quiet
if errorlevel 1 (
    echo   WARNING: PyTorch CPU wheel failed. PII Guard stages 2+3 will be disabled.
    echo   Retry manually: .venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
)

call :pip_install transformers sentence-transformers numpy --quiet
call :pip_install presidio-analyzer presidio-anonymizer spacy --quiet

python -m spacy download en_core_web_sm
if errorlevel 1 (
    echo   WARNING: spaCy model download failed.
    echo   Retry: .venv\Scripts\activate ^&^& python -m spacy download en_core_web_sm
)

REM ── Desktop launcher + optional Windows connectors ───────────────────────────
echo [5/5] Installing desktop launcher and connectors...
call :pip_install pywebview pystray --quiet
call :pip_install pywin32 pyodbc --quiet
if errorlevel 1 (
    echo   WARNING: pywin32 / pyodbc optional — Outlook and SQL Server features disabled.
)

REM ── Local dirs ───────────────────────────────────────────────────────────────
if not exist logs  mkdir logs
if not exist models mkdir models

REM ── Download HuggingFace models ───────────────────────────────────────────────
echo.
echo [+] Downloading AI models ^(~670MB^)...
echo     dslim/bert-base-NER       ~400MB   PII detection
echo     nli-deberta-v3-small      ~180MB   PHI judge
echo     all-MiniLM-L6-v2          ~90MB    Memory embeddings
echo.
python download_models.py
if errorlevel 1 (
    echo   WARNING: Some models failed. Retry: python download_models.py
)

REM ── Desktop shortcut ─────────────────────────────────────────────────────────
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
exit /b 0


REM ═══════════════════════════════════════════════════════════════════════════
REM  :pip_install  — SSL-aware pip wrapper
REM
REM  Runs pip with the supplied arguments.  If the install fails with an
REM  SSL/certificate error, the user is shown an explanation and asked
REM  whether to retry using --trusted-host (bypass SSL verification).
REM  Once approved, the bypass is used for the rest of the session.
REM ═══════════════════════════════════════════════════════════════════════════
:pip_install
    set _pip_args=%*
    set _pip_log=%TEMP%\orions_pip_%RANDOM%.log

    REM If bypass already approved, skip straight to trusted-host install
    if "%SSL_BYPASS%"=="1" goto :pip_trusted

    REM Normal install — capture stderr+stdout to detect SSL errors
    pip install %_pip_args% > "%_pip_log%" 2>&1
    if not errorlevel 1 (
        del /f /q "%_pip_log%" 2>nul
        exit /b 0
    )

    REM Check if the failure mentions SSL / certificate issues
    findstr /i "ssl certificate CERTIFICATE_VERIFY_FAILED" "%_pip_log%" >nul 2>&1
    if errorlevel 1 (
        REM Non-SSL failure — print the log and return the error
        type "%_pip_log%"
        del /f /q "%_pip_log%" 2>nul
        exit /b 1
    )

    REM SSL error detected — explain and ask
    del /f /q "%_pip_log%" 2>nul
    echo.
    echo   +------------------------------------------------------+
    echo   ^|  SSL certificate error detected during pip install.  ^|
    echo   ^|  This is common on corporate / enterprise networks   ^|
    echo   ^|  that use a TLS-inspecting proxy.                    ^|
    echo   +------------------------------------------------------^|
    echo   ^|  Bypass option adds these flags to pip:              ^|
    echo   ^|    --trusted-host pypi.org                           ^|
    echo   ^|    --trusted-host files.pythonhosted.org             ^|
    echo   ^|                                                       ^|
    echo   ^|  WARNING: bypassing SSL reduces security.  Only do   ^|
    echo   ^|  this on a network you trust.                        ^|
    echo   +------------------------------------------------------+
    echo.
    set /p _ssl_ans=  Allow SSL bypass for this install session? [y/N]:
    if /i "!_ssl_ans!"=="y"   goto :pip_approve_bypass
    if /i "!_ssl_ans!"=="yes" goto :pip_approve_bypass

    echo   SSL bypass declined. You can configure a CA bundle with:
    echo     pip config set global.cert "C:\path\to\your-ca-bundle.pem"
    exit /b 1

:pip_approve_bypass
    set SSL_BYPASS=1
    echo   SSL bypass enabled for this session.
    echo.

:pip_trusted
    pip install ^
        --trusted-host pypi.org ^
        --trusted-host files.pythonhosted.org ^
        %_pip_args%
    exit /b %ERRORLEVEL%
