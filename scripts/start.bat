@echo off
rem Start Tokriva console (double-click to run).
rem Equals: python scripts/start.py
setlocal
cd /d "%~dp0.."
python scripts\start.py %*
if errorlevel 1 pause
endlocal
