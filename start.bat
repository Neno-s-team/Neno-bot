@echo off
rem One command to run Neno on Windows: creates venv, installs dependencies, then runs run.py.
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10+ is required. Get it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
    pause
    exit /b 1
)
if not exist venv\Scripts\python.exe (
    echo [1/2] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 ( pause & exit /b 1 )
)
fc /b requirements.txt venv\.requirements >nul 2>nul
if errorlevel 1 (
    echo [2/2] Installing dependencies ^(first run only^)...
    venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
    if errorlevel 1 ( pause & exit /b 1 )
    copy /y requirements.txt venv\.requirements >nul
)
venv\Scripts\python run.py %*
if errorlevel 1 pause
