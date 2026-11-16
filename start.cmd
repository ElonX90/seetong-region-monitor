@echo off
setlocal
cd /d "%~dp0"
set "project_python=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%project_python%" set "project_python=python"
"%project_python%" -c "import sys, struct, platform; assert sys.version_info[:2] in [(3,11),(3,12),(3,13)] and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64')" >nul 2>&1
if not errorlevel 1 goto launch
echo Installing Python 3.12 x64 with WinGet...
winget install --id Python.Python.3.12 --exact --source winget --architecture x64 --scope user --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto failed
set "project_python=%LocalAppData%\Programs\Python\Python312\python.exe"
:launch
"%project_python%" -B "%~dp0outputs\seetong_setup.py"
if not errorlevel 1 exit /b 0
:failed
echo Startup failed. See the message above.
pause
exit /b 1
