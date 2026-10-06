@echo off
setlocal
title Build PokeRarity EXE v0.2.3
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install pyinstaller
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name PokeRarity ^
  --add-data "assets;assets" ^
  --collect-all rapidocr --collect-all onnxruntime ^
  app.py

if errorlevel 1 goto :failed

echo.
echo Build finished.
echo Check dist\PokeRarity\
echo.
pause
exit /b 0

:failed
echo.
echo Build failed.
pause
exit /b 1
