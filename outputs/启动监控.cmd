@echo off
setlocal
set "monitor_python=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
if exist "%monitor_python%" goto launch
set "monitor_python=python"
:launch
"%monitor_python%" -B "%~dp0seetong-monitor.py"
if errorlevel 1 pause
