@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title MikroFIX v30 DEV - namestitev

echo.
echo MikroFIX v30 DEV - priprava Windows okolja
echo ===========================================

set "PYTHON_EXE="
if exist "%LOCALAPPDATA%\Python\pythoncore-3.13-64\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Python\pythoncore-3.13-64\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Python\bin\python3.13.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Python\bin\python3.13.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PYTHON_EXE goto python_direct
where py >nul 2>&1
if errorlevel 1 goto python_missing
py -3.13 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if errorlevel 1 goto python_wrong
set "PYTHON_LAUNCHER=1"
goto python_ready

:python_direct
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if errorlevel 1 goto python_wrong

:python_ready
if not exist ".venv\Scripts\python.exe" (
    echo Ustvarjam loceno okolje v30...
    if defined PYTHON_LAUNCHER (
        py -3.13 -m venv .venv
    ) else (
        "%PYTHON_EXE%" -m venv .venv
    )
    if errorlevel 1 goto install_failed
)

".venv\Scripts\python.exe" -c "from mikrofix_tk_bootstrap import prepare_tcl_tk; prepare_tcl_tk(); import tkinter as tk; root=tk.Tk(); root.withdraw(); root.destroy()" >nul 2>&1
if errorlevel 1 goto tcltk_missing

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto cpp_missing
"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath >nul 2>&1
if errorlevel 1 goto cpp_missing

echo Namescam potrebne dodatke. To lahko traja nekaj minut...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto install_failed
".venv\Scripts\python.exe" -m pip install --no-cache-dir -r requirements-v30.txt
if errorlevel 1 goto install_failed

echo.
echo Namestitev v30 DEV je koncana.
echo Podatki v29 niso bili spremenjeni.
pause
exit /b 0

:python_missing
echo MANJKA: Python 3.13 64-bit iz python.org.
echo Pri namestitvi obdrzi Python Launcher in Tcl/Tk.
pause
exit /b 1

:python_wrong
echo Python obstaja, vendar ni Python 3.13 64-bit.
pause
exit /b 1

:tcltk_missing
echo Python je najden, vendar Tcl/Tk ne deluje. Uporabi Repair za Python 3.13.
pause
exit /b 3

:cpp_missing
echo MANJKA: Microsoft C++ Build Tools 14.0 ali novejsi.
echo V Visual Studio Installerju izberi Desktop development with C++.
pause
exit /b 2

:install_failed
echo Namestitev ni uspela. Ce je omenjen lzfse, preveri C++ Build Tools.
pause
exit /b 1
