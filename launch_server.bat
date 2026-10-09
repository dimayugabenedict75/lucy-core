@echo off
REM Lucy Core launcher with auto-reload (--reload), so code changes restart the
REM server automatically, including /api/server/restart and /api/cua.
REM Paths are relative to this file, so the folder can live anywhere.
set "LUCY_ROOT=%~dp0"
if "%LUCY_ROOT:~-1%"=="\" set "LUCY_ROOT=%LUCY_ROOT:~0,-1%"
set "PYTHONPATH=%LUCY_ROOT%\src"
cd /d "%LUCY_ROOT%"

REM Python to use: set LUCY_PYTHON to override. Default is the agents-harness venv
REM in your user profile; if that does not exist, fall back to the py launcher.
if not defined LUCY_PYTHON set "LUCY_PYTHON=%USERPROFILE%\venvs\agents-harness\Scripts\python.exe"
if exist "%LUCY_PYTHON%" (
    "%LUCY_PYTHON%" -m uvicorn lucy.server.api:app --host 0.0.0.0 --port 8090 --log-level info --reload
) else (
    echo [warn] "%LUCY_PYTHON%" not found - falling back to py -3.11
    py -3.11 -m uvicorn lucy.server.api:app --host 0.0.0.0 --port 8090 --log-level info --reload
)
pause
