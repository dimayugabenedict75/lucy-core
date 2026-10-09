@echo off
REM Lucy Core launcher (single process, no auto-reload).
REM Paths are relative to this file, so the folder can live anywhere.
set "LUCY_ROOT=%~dp0"
if "%LUCY_ROOT:~-1%"=="\" set "LUCY_ROOT=%LUCY_ROOT:~0,-1%"
REM Uses the system Python with PYTHONPATH to bypass the Hermes venv's editable
REM install of agents-harness, which shadows lucy.server.
set "PYTHONPATH=""
cd /d "%LUCY_ROOT%"
uv run --no-project python -m lucy.server.server
pause
