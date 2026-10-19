@echo off
rem Accept YOUR changes to Lucy's safety files (permissions.yaml, src/lucy/safety, ...). Asks you to type SEAL.
rem Other uses: seal.bat status ^| seal.bat backups ^| seal.bat restore ID
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
cd /d "%~dp0src"
if "%~1"=="" ("%PY%" -m lucy.safety seal) else ("%PY%" -m lucy.safety %*)
cd /d "%~dp0"
pause
