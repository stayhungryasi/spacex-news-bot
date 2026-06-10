@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py" >> "%~dp0run.log" 2>&1
