@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  exit /b 1
)
echo Restoring .env, users and the Google key from secrets.zip - you will be asked for the passphrase.
".venv\Scripts\python.exe" scripts\secrets_tool.py unpack
