@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install it from python.org, then run this again.
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip PySide6 pyinstaller
pyinstaller --noconfirm --noconsole --onedir --name PaperGlass paperglass.py
echo.
echo Done. Run dist\PaperGlass\PaperGlass.exe  (or use make_installer.bat to build a proper installer)
pause
