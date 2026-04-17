@echo off
REM Orion's Belt — Create desktop shortcut
REM Run once after setup, or re-run to update the shortcut

cd /d "%~dp0"

REM Generate icon first
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
    python create_icon.py
) else (
    echo WARNING: .venv not found. Run setup.bat first.
    echo Shortcut will be created without a custom icon.
)

REM Create the .lnk shortcut via PowerShell
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
  "Write-Host '  Shortcut created on Desktop: Orions Belt.lnk'"

echo.
echo  Done! Look for "Orions Belt" on your Desktop.
echo.
pause
