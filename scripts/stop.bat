@echo off
rem Stop Tokriva console (double-click to run).
rem Equals: python scripts/stop.py
setlocal
cd /d "%~dp0.."
python scripts\stop.py %*
if errorlevel 1 pause
endlocal
