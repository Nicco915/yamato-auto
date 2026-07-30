@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title Yamato Doc Automation - Setup

cd /d "%~dp0"

echo ============================================================
echo  Yamato Doc Automation - Dependency Setup (Windows)
echo ============================================================
echo.

REM ---------- 1. Check Python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Please install Python 3.10+ from https://www.python.org/
    echo         and check "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Found Python %PYVER%

REM ---------- 2. Create virtual environment ----------
if not exist ".venv" (
    echo [..] Creating virtual environment .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment .venv already exists.
)

set VENV_PY=.venv\Scripts\python.exe

REM ---------- 3. Upgrade pip ----------
echo [..] Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo [WARN] pip upgrade failed, continuing with existing pip.
)

REM ---------- 4. Install dependencies ----------
echo [..] Installing dependencies from requirements.txt ...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.

REM ---------- 5. Prepare .env ----------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [OK] .env created from .env.example - please edit it before starting.
    ) else (
        echo [WARN] .env.example not found, skipped .env creation.
    )
) else (
    echo [OK] .env already exists.
)

REM ---------- 6. Prepare runtime directories ----------
if not exist "app\data"   mkdir "app\data"
if not exist "app\output" mkdir "app\output"
echo [OK] Runtime directories ready: app\data, app\output

REM ---------- 7. LibreOffice (required for .doc/.docx extraction on Windows) ----------
set SOFFICE_FOUND=0
if defined SOFFICE_PATH if exist "%SOFFICE_PATH%" set SOFFICE_FOUND=1
where soffice >nul 2>&1 && set SOFFICE_FOUND=1
if exist "C:\Program Files\LibreOffice\program\soffice.exe" set SOFFICE_FOUND=1
if exist "C:\Program Files (x86)\LibreOffice\program\soffice.exe" set SOFFICE_FOUND=1

if "%SOFFICE_FOUND%"=="1" (
    echo [OK] LibreOffice detected.
) else (
    echo [..] LibreOffice not found. It is required to extract .doc/.docx files.
    echo [..] Installing from China mirrors ^(TUNA / Aliyun^) ...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_libreoffice.ps1"
    if errorlevel 1 (
        echo [WARN] Mirror install failed, trying winget ...
        where winget >nul 2>&1
        if errorlevel 1 (
            echo [WARN] winget not available.
        ) else (
            winget install --id TheDocumentFoundation.LibreOffice -e --accept-source-agreements --accept-package-agreements
        )
    )
    REM Final check after install attempts
    set SOFFICE_FOUND=0
    where soffice >nul 2>&1 && set SOFFICE_FOUND=1
    if exist "C:\Program Files\LibreOffice\program\soffice.exe" set SOFFICE_FOUND=1
    if exist "C:\Program Files (x86)\LibreOffice\program\soffice.exe" set SOFFICE_FOUND=1
    if "%SOFFICE_FOUND%"=="1" (
        echo [OK] LibreOffice installed.
    ) else (
        echo [WARN] LibreOffice is still not available.
        echo        Please install it manually: https://www.libreoffice.org/download
        echo        ^(Default path: C:\Program Files\LibreOffice\program\soffice.exe^)
        echo        If installed to a custom path, set SOFFICE_PATH in .env to soffice.exe.
    )
)

echo.
echo ============================================================
echo  Setup complete. Next steps:
echo    1. Edit .env ^(API key, UPSTREAM_ROOT, paths^)
echo    2. Run start.bat to launch the service
echo ============================================================
pause
endlocal
