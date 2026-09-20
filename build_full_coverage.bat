@echo off
setlocal
cd /d "%~dp0"
echo.
echo ADVANCED and optional: lets PaperGlass draw texture, lamp and flash over the Start menu and Quick Settings.
echo It builds a special copy, then installs it to Program Files with a certificate made on this PC.
echo Do NOT run the special copy from the build folder; Windows refuses to start it there.
echo.
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install it from python.org, then run this again.
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip PySide6 pyinstaller
if exist dist_ui rmdir /s /q dist_ui
pyinstaller --noconfirm --noconsole --onedir --uac-uiaccess --name PaperGlass --distpath dist_ui --workpath build_ui --specpath build_ui "%~dp0paperglass.py"
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0setup_full_coverage.ps1\"'"
