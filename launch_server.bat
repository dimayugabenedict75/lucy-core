@echo off
REM Lucy Core launcher — uses the agents-harness venv Python directly
REM (avoids dependency on `py -3.11` launcher which may not be installed).
set PYTHONPATH=C:\Users\dimay\Lucy\Lucy_Core\src
cd /d C:\Users\dimay\Lucy\Lucy_Core
"C:\Users\dimay\Lucy\venvs\agents-harness\Scripts\python.exe" -m uvicorn lucy.server.api:app --host 0.0.0.0 --port 8090 --log-level info
