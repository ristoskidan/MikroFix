@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem Program je lahko na USB, poslovne baze pa morajo ostati na lokalnem disku.
set "MIKROFIX_DATA_DIR=%USERPROFILE%\Documents\MikroFixData_V30"

if not exist ".venv\Scripts\python.exe" (
    echo MikroFIX v30 okolje na USB ni namesceno.
    echo Najprej zazeni namesti_v30.bat iz te mape.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" mikrofix.py
if errorlevel 1 pause
