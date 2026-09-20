@echo off
setlocal
cd /d "%~dp0"
echo.
echo === PaperGlass installer builder ===
echo.

where py >nul 2>nul
if errorlevel 1 goto nopython

if not exist .venv py -3 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip PySide6 pyinstaller
if errorlevel 1 goto fail

if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist paperglass.ico del paperglass.ico
python paperglass.py --make-icon paperglass.ico

set "ICONARG="
if exist paperglass.ico set "ICONARG=--icon paperglass.ico"
pyinstaller --noconfirm --noconsole --onedir %ICONARG% --name PaperGlass paperglass.py
if errorlevel 1 goto fail
if not exist "dist\PaperGlass\PaperGlass.exe" goto fail

call :findiscc
if defined ISCC goto compile
echo Inno Setup (the free tool that makes installers) was not found. Installing it with winget...
winget install -e --id JRSoftware.InnoSetup --accept-source-agreements --accept-package-agreements
call :findiscc
if not defined ISCC goto noinno

:compile
"%ISCC%" installer.iss
if errorlevel 1 goto fail
echo.
echo Done! Your installer is: installer\PaperGlass-Setup.exe
explorer installer
pause
exit /b 0

:findiscc
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
exit /b 0

:nopython
echo Python 3.10 or newer is required. Install it from python.org (tick "Add python.exe to PATH"), then run this again.
pause
exit /b 1

:noinno
echo Could not install Inno Setup automatically. Install it from https://jrsoftware.org/isdl.php and run this again.
pause
exit /b 1

:fail
echo.
echo Something went wrong. Scroll up to see the message.
pause
exit /b 1
