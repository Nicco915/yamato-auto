@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title Yamato Doc Automation - Server

cd /d "%~dp0"

echo ============================================================
echo  Yamato Doc Automation - Start Service
echo ============================================================
echo.

REM ---------- 1. Check virtual environment ----------
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo         Please run setup.bat first.
    pause
    exit /b 1
)

REM ---------- 2. Check .env ----------
if not exist ".env" (
    echo [WARN] .env not found. Creating from .env.example ...
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo        Please edit .env and restart.
        pause
        exit /b 1
    ) else (
        echo [ERROR] .env.example not found either.
        pause
        exit /b 1
    )
)

REM ---------- 3. UTF-8 environment ----------
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM ---------- 4. Port (default 8000, override: start.bat 8001) ----------
set PORT=%1
if "%PORT%"=="" set PORT=8000

echo [OK] Starting FastAPI server on http://127.0.0.1:%PORT%
echo      Press Ctrl+C to stop.
echo.

REM ---------- 5. Auto-open browser (delayed 3s, wait for uvicorn ready) ----------
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:%PORT%/"

.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port %PORT%

if errorlevel 1 (
    echo.
    echo [ERROR] Server exited with an error. Check the log above.
    pause
)
endlocal
