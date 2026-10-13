@echo off
rem Double-click to start (Windows). Use the Quit button on the control panel to exit.
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python -m simul_interp %*
if errorlevel 1 pause
