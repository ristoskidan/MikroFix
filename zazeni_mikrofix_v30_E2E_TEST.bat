@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title MikroFIX v30 - IZOLIRANI E2E TEST

set "MIKROFIX_ALLOW_FURS_PRODUCTION="
set "MIKROFIX_CONFIG=%~dp0mikrofix_settings_e2e_test.json"
set "MIKROFIX_E2E_TEST=YES"
set "MIKROFIX_SKIP_FILE_PROTECTION=1"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" if exist "C:\Users\MikroFix\Desktop\MikroFIX_Service_v30_DEV_TEST_R2_2026-08-30\MikroFIX_Service\.venv\Scripts\python.exe" set "PYTHON_EXE=C:\Users\MikroFix\Desktop\MikroFIX_Service_v30_DEV_TEST_R2_2026-08-30\MikroFIX_Service\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo MikroFIX v30 okolje ni namesceno. Najprej zazeni namesti_v30.bat.
    pause
    exit /b 1
)

"%PYTHON_EXE%" pripravi_v30_e2e_test.py
if errorlevel 1 (
    echo.
    echo E2E TEST baza ni pripravljena. Program se zaradi varnosti ne bo zagnal.
    pause
    exit /b 2
)

"%PYTHON_EXE%" mikrofix.py
if errorlevel 1 pause
