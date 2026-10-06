@echo off
setlocal
title PokeRarity Setup v0.2.3
cd /d "%~dp0"

echo.
echo ==========================================
echo      PokeRarity v0.2.3 Setup
echo ==========================================
echo.
echo PokeRarity v0.2.3 uses the same RapidOCR package stack as PokeScan.
echo.
echo This setup will use Python 3.12 in its own
echo private virtual environment (.venv).
echo.

REM Check for Python 3.12
py -3.12 --version >nul 2>nul
if errorlevel 1 (
    echo Python 3.12 was not found.
    echo.
    where winget >nul 2>nul
    if errorlevel 1 (
        echo Windows Package Manager ^(winget^) was not found.
        echo Please install Python 3.12 x64 from python.org,
        echo then run setup.bat again.
        echo.
        pause
        exit /b 1
    )

    echo Installing Python 3.12 using winget...
    echo.
    winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements

    echo.
    echo Re-checking Python 3.12...
    py -3.12 --version >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Python 3.12 installation finished, but the Python
        echo launcher has not refreshed yet.
        echo.
        echo Close this window, then run setup.bat again.
        echo.
        pause
        exit /b 1
    )
)

echo Found:
py -3.12 --version
echo.

if exist ".venv\Scripts\python.exe" (
    echo Existing PokeRarity environment found.
) else (
    echo Creating private Python 3.12 environment...
    py -3.12 -m venv .venv
    if errorlevel 1 goto :failed
)

echo.
echo Updating pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :failed

echo.
echo Installing PokeRarity dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Building local Pokemon card database...
echo This downloads the public TCGdex cards-database snapshot from GitHub.
echo It may take a few minutes the first time.
echo.
".venv\Scripts\python.exe" build_cache.py
echo.

echo.
echo ==========================================
echo Setup complete!
echo ==========================================
echo.
echo You can now run run.bat.
echo.
pause
exit /b 0

:failed
echo.
echo ==========================================
echo Setup failed.
echo ==========================================
echo.
echo Please copy the last error shown above if
echo you need help.
echo.
pause
exit /b 1
