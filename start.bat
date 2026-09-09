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

REM ---------- 5. Check port availability (bind probe) ----------
echo [INFO] Checking port %PORT% ...
.venv\Scripts\python.exe -c "import socket, sys; s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', %PORT%)); s.close()" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Port %PORT% is not available ^(in use or reserved by Windows^).
    echo        Scanning for an alternative port ...
    set FOUND=0
    for /L %%i in (1,1,50) do (
        if !FOUND! equ 0 (
            set /a ALT_PORT=!PORT!+%%i
            .venv\Scripts\python.exe -c "import socket, sys; s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', !ALT_PORT!)); s.close()" >nul 2>&1
            if !errorlevel! equ 0 (
                set PORT=!ALT_PORT!
                set FOUND=1
            )
        )
    )
    if !FOUND! equ 0 (
        echo [ERROR] No available port found between %PORT% and !ALT_PORT!.
        echo         Please manually specify a port, e.g. start.bat 8080
        pause
        exit /b 1
    )
    echo [OK] Auto-switched to available port: %PORT%
)

echo [OK] Starting FastAPI server on http://127.0.0.1:%PORT%
echo      Press Ctrl+C to stop.
echo.

REM ---------- 6. Auto-open browser (delayed 3s, wait for uvicorn ready) ----------
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:%PORT%/"

.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port %PORT%

if errorlevel 1 (
    echo.
    echo [ERROR] Server exited with an error. Check the log above.
    pause
)
endlocal
