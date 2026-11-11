@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo Run setup_windows.cmd first.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m src.gldrubf_bot.windows paper
set "BOT_EXIT=%ERRORLEVEL%"
pause >nul
exit /b %BOT_EXIT%
