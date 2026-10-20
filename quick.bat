@echo off
rem Lucy quick entry: Ctrl+Alt+Space opens a small box anywhere (LUCY_QUICK_KEY / LUCY_URL to change)
cd /d "%~dp0src"
start "" ..\.venv\Scripts\pythonw.exe -m lucy.desk.quick
