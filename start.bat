@echo off
title Trading Agent
cd /d "%~dp0"

echo(====================================
echo(  Trading Agent - Launcher
echo(====================================
echo(

:: Check install
if exist .installed (
    echo([OK] Dependencies already installed.
    echo(
    goto :RUN_GUI
)

echo([1/2] Installing requirements (first run)...
echo(------------------------------------
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo(
    echo([ERROR] Installation failed!
    pause
    exit /b 1
)

:: Create install marker
echo. > .installed
echo([OK] Installation complete!

:RUN_GUI
echo(
echo([2/2] Starting GUI...
echo(------------------------------------
python setup_gui.py

echo(
echo(====================================
echo(  Finished.
echo(====================================
pause