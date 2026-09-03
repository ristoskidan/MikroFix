@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title MikroFIX v30 DEV - preverjanje
if not exist ".venv\Scripts\python.exe" (
    echo MikroFIX v30 okolje ni namesceno. Najprej zazeni namesti_v30.bat.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" preveri_v30_pred_testom.py
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
