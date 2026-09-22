@echo off
REM Lucy Core launcher — uses the system Python with PYTHONPATH to bypass
REM the Hermes venv's editable install of agents-harness which shadows lucy.server.
set PYTHONPATH=C:\Users\dimay\Lucy\Lucy_Core\src
cd /d C:\Users\dimay\Lucy\Lucy_Core
py -3.11 -m lucy.server.server
pause
