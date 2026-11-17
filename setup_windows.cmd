@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (set "BOTPY=python") else (set "BOTPY=py -3")
%BOTPY% -c "import sys;sys.exit(sys.version_info < (3,11))"
if errorlevel 1 (
 echo Install Python 3.11 or newer from python.org with PATH enabled.
 pause
 exit /b 1
)
if not exist ".venv\Scripts\python.exe" %BOTPY% -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 (
 echo Tests FAILED. Do not start PAPER until resolved.
 pause
 exit /b 1
)
echo Setup ready. Standard library only. No token was requested.
pause
