@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo client=launcher http_status=unknown category=setup_required
 pause >nul
 exit /b 1
)
".venv\Scripts\python.exe" -m src.gldrubf_bot.diagnose_api
set "DIAG_EXIT=%ERRORLEVEL%"
pause >nul
exit /b %DIAG_EXIT%
