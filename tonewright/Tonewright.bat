@echo off
rem Starts Tonewright. The first run creates a private Python environment next to this file.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Setting up Tonewright for the first time. This takes a minute or two...
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Python 3.10 or newer was not found.
        echo Install it with:  winget install Python.Python.3.13
        echo then double-click Tonewright.bat again.
        pause
        exit /b 1
    )
    python -m venv .venv
    if errorlevel 1 ( echo Could not create the Python environment. & pause & exit /b 1 )
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 ( echo Installing the requirements failed. Check your internet connection. & pause & exit /b 1 )
)

start "" ".venv\Scripts\pythonw.exe" -m tonewright %*
