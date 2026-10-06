@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo PokeRarity has not been set up yet.
    echo Please run setup.bat first.
    echo.
    pause
    exit /b 1
)

REM Testing build: keep console visible so OCR/API activity and errors show.
".venv\Scripts\python.exe" app.py
echo.
echo PokeRarity closed.
pause
